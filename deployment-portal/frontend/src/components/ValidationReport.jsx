import React from 'react'

const OVERALL = {
  passed: { cls: 'badge-done', label: 'Approve-ready', icon: '✓' },
  warnings: { cls: 'badge-pending', label: 'Needs attention', icon: '⚠' },
  failed: { cls: 'badge-failed', label: 'Blocked', icon: '✕' },
}

const ITEM_ICON = { pass: '✅', warn: '⚠️', fail: '❌' }
const CHECK_COLOR = { pass: 'var(--green)', warn: 'var(--amber, #b45309)', fail: 'var(--red)' }

const SECTION_META = {
  build: { icon: '🔀' },
  yaml: { icon: '📄' },
  db: { icon: '🗄️' },
  phrases: { icon: '💬' },
}

// Minimal, safe inline renderer: **bold**, [text](url) and bare URLs -> links.
function renderInline(text, keyBase) {
  const nodes = []
  let remaining = text
  let i = 0
  const pattern = /(\*\*(.+?)\*\*)|(\[([^\]]+)\]\((https?:\/\/[^\s)]+)\))|(https?:\/\/[^\s)]+)/
  while (remaining.length) {
    const m = remaining.match(pattern)
    if (!m) {
      nodes.push(remaining)
      break
    }
    if (m.index > 0) nodes.push(remaining.slice(0, m.index))
    if (m[1]) {
      nodes.push(<strong key={`${keyBase}-b${i}`}>{m[2]}</strong>)
    } else if (m[3]) {
      nodes.push(<a key={`${keyBase}-l${i}`} href={m[5]} target="_blank" rel="noreferrer">{m[4]}</a>)
    } else if (m[6]) {
      nodes.push(<a key={`${keyBase}-u${i}`} href={m[6]} target="_blank" rel="noreferrer">{m[6]}</a>)
    }
    remaining = remaining.slice(m.index + m[0].length)
    i += 1
  }
  return nodes
}

function Markdown({ text }) {
  const lines = (text || '').split('\n')
  const blocks = []
  let list = null

  const flush = () => {
    if (list) {
      blocks.push(
        list.ordered
          ? <ol key={`ol-${blocks.length}`} className="md-list">{list.items}</ol>
          : <ul key={`ul-${blocks.length}`} className="md-list">{list.items}</ul>,
      )
      list = null
    }
  }

  lines.forEach((raw, idx) => {
    const line = raw.trimEnd()
    if (!line.trim()) { flush(); return }
    if (line.startsWith('## ')) {
      flush()
      blocks.push(<h4 key={`h-${idx}`} className="md-h">{renderInline(line.slice(3), `h${idx}`)}</h4>)
      return
    }
    const ol = line.match(/^\s*(\d+)\.\s+(.*)$/)
    const ul = line.match(/^\s*[-*]\s+(.*)$/)
    if (ol) {
      if (!list || !list.ordered) { flush(); list = { ordered: true, items: [] } }
      list.items.push(<li key={`li-${idx}`}>{renderInline(ol[2], `li${idx}`)}</li>)
      return
    }
    if (ul) {
      if (!list || list.ordered) { flush(); list = { ordered: false, items: [] } }
      list.items.push(<li key={`li-${idx}`}>{renderInline(ul[1], `li${idx}`)}</li>)
      return
    }
    flush()
    blocks.push(<p key={`p-${idx}`} className="md-p">{renderInline(line, `p${idx}`)}</p>)
  })
  flush()
  return <div className="md">{blocks}</div>
}

export default function ValidationReport({ task, onRevalidate, revalidating }) {
  const status = task.validation_status
  const report = task.validation_report
  const isRunning = status === 'pending' || status === 'running'

  // First validation: no report yet — show spinner only.
  if (!report && isRunning) {
    return (
      <div className="card validation-card">
        <div className="validation-head">
          <p className="card-title" style={{ marginBottom: 0 }}>🤖 AI validation report</p>
        </div>
        <p className="card-sub" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className="spinner" /> Running GitSpace validation…
        </p>
      </div>
    )
  }

  if (!report) {
    return (
      <div className="card validation-card">
        <p className="card-title" style={{ marginBottom: 0 }}>🤖 AI validation report</p>
        <p className="card-sub">No validation report yet.</p>
      </div>
    )
  }

  const overall = OVERALL[report.overall_status] || OVERALL.warnings
  const s = report.stats || { checked: 0, passed: 0, warnings: 0, failed: 0 }
  const showRunningBanner = isRunning || revalidating

  return (
    <div className="card validation-card">
      {showRunningBanner && (
        <p className="card-sub" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
          <span className="spinner" /> Re-validation in progress… showing last report until complete.
        </p>
      )}
      <div className="validation-head">
        <div>
          <p className="card-title" style={{ marginBottom: 2 }}>
            🤖 AI validation report <span className={`badge ${overall.cls}`}>{overall.icon} {overall.label}</span>
          </p>
          <p className="card-sub" style={{ marginBottom: 0 }}>
            {s.passed} passed · {s.warnings} warning(s) · {s.failed} failed of {s.checked} ·{' '}
            {report.ai_used ? `AI: ${report.ai_model}` : 'AI offline (deterministic report)'}
          </p>
        </div>
        <button type="button" className="btn" onClick={onRevalidate} disabled={revalidating}>
          {revalidating ? <><span className="spinner" /> Re-validating…</> : '↻ Re-validate'}
        </button>
      </div>

      {report.summary_markdown && (
        <div className="validation-briefing">
          <Markdown text={report.summary_markdown} />
        </div>
      )}

      {(report.sections || []).map((g) => (
        <div key={g.section} className="validation-section">
          <p className="validation-section-title">
            {(SECTION_META[g.section] || {}).icon || '🔧'} {g.title}
            <span className={`badge ${OVERALL[{ pass: 'passed', warn: 'warnings', fail: 'failed' }[g.status]].cls}`}>
              {ITEM_ICON[g.status]} {g.status}
            </span>
          </p>
          {g.items.map((it, i) => (
            <div key={i} className="validation-item">
              <div className="validation-item-head">
                <span>{ITEM_ICON[it.status]}</span>
                <strong>{it.label}</strong>
                <span className="validation-subtype">{it.sub_type}</span>
              </div>
              <ul className="validation-checks">
                {it.checks.map((c, ci) => (
                  <li key={ci} style={{ color: CHECK_COLOR[c.status] || 'inherit', whiteSpace: 'pre-line' }}>
                    {g.section === 'db' ? (
                      <span style={{ color: 'var(--text-secondary)' }}>{c.detail}</span>
                    ) : (
                      <>
                        <span style={{ fontWeight: 600 }}>{c.name}:</span>{' '}
                        <span style={{ color: 'var(--text-secondary)' }}>{c.detail}</span>
                      </>
                    )}
                  </li>
                ))}
              </ul>
              {it.merge && (
                <p className="validation-meta">
                  Merge request !{it.merge.iid} · {it.merge.source_branch} → {it.merge.target_branch}
                  {it.merge.mergeable === false && it.merge.conflicts?.length
                    ? ` · conflicts: ${it.merge.conflicts.join(', ')}` : ''}
                </p>
              )}
              <div className="validation-links">
                {it.baseline?.release && (
                  <span className="validation-meta" style={{ marginRight: 8 }}>
                    Baseline: {it.baseline.release}
                  </span>
                )}
                {Object.entries(it.urls || {}).map(([k, url]) => {
                  if (!url || k === 'file' || k === 'baseline_file') return null
                  const label = k.endsWith('.sql') ? k
                    : k === 'merge_request' ? 'Merge request'
                    : k === 'compare' ? 'Compare'
                    : k.replace(/_/g, '.')
                  return (
                    <a key={k} href={url} target="_blank" rel="noreferrer" className="validation-link">
                      {label} ↗
                    </a>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      ))}

      {report.steps?.length > 0 && (
        <div className="validation-steps">
          <p className="validation-section-title">Recommended next steps</p>
          <ol className="md-list">
            {report.steps.map((st, i) => <li key={i}>{st}</li>)}
          </ol>
        </div>
      )}
    </div>
  )
}
