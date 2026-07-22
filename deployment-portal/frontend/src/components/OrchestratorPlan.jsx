import React, { useState, useRef, useEffect, useCallback } from 'react'

const SECTION_ICONS = { build: '🔨', yaml: '📄', db: '🗄️', phrases: '💬' }

/**
 * OrchestratorPlan
 *
 * @param plan      task.orchestrator_plan  ({ phases, total_sub_tasks, estimated_minutes })
 * @param subTasks  flat list of sub-task objects
 * @param role      current user's role — controls detail level:
 *                   developer → status only (no step detail / params / logs)
 *                   dev_lead/qa → phase layout + step detail (no params / logs)
 *                   devops → full detail (steps, detail text, Jenkins params, logs, retry)
 * @param onRetry   async (subTaskId) => void — re-run a failed build (DevOps only)
 */
export default function OrchestratorPlan({ plan, subTasks, role = 'developer', preview = false, onRetry }) {
  const [expanded, setExpanded] = useState(null)

  if (!plan || !plan.phases) return null

  const stMap = Object.fromEntries((subTasks || []).map((st) => [st.sub_task_id, st]))

  return (
    <div className="orchestrator-plan">
      <div className="op-header">
        <h3 className="op-title">Orchestrator Plan</h3>
        <span className="op-meta">
          {plan.total_sub_tasks} sub-tasks · ~{plan.estimated_minutes} min estimated
        </span>
      </div>

      {plan.phases.map((phase, pi) => (
        <div key={phase.phase} className="op-phase">
          {pi > 0 && <div className="op-phase-arrow">↓ after phase {plan.phases[pi - 1].phase} completes</div>}

          <div className="op-phase-header">
            <span className="op-phase-num">Phase {phase.phase}</span>
            <span className="op-phase-label">{phase.label}</span>
            {phase.parallel && phase.sub_task_ids.length > 1 && (
              <span className="op-parallel-badge">⟂ parallel</span>
            )}
          </div>

          <div className={`op-phase-lanes ${phase.parallel ? 'parallel' : 'sequential'}`}>
            {phase.sub_task_ids.map((stId) => {
              const st = stMap[stId]
              if (!st) return null
              return (
                <SubTaskCard
                  key={stId}
                  st={st}
                  role={role}
                  preview={preview}
                  isExpanded={expanded === stId}
                  onToggle={() => setExpanded(expanded === stId ? null : stId)}
                  onRetry={onRetry}
                />
              )
            })}
          </div>
        </div>
      ))}
    </div>
  )
}

function SubTaskCard({ st, role, preview, isExpanded, onToggle, onRetry }) {
  const showStepDetail = role !== 'developer'
  const showParamsAndLogs = role === 'devops'
  const showJobCard = role === 'devops'
  const canRetry = role === 'devops' && typeof onRetry === 'function' && st.status === 'failed'
  const isAborted = st.failure_category === 'user_aborted'

  const attempts = Array.isArray(st.build_attempts) ? st.build_attempts : []
  const latest = attempts.length ? attempts[attempts.length - 1] : null
  const plannedRuns = (st.json_deployments || st.yaml_deployments || []).map((dep, i) => ({
    index: i + 1,
    label: dep.action
      ? `${dep.update || st.section} · ${dep.action}${dep.filename ? ` (${dep.filename})` : ''}`
      : dep.update || dep.filename || `Run ${i + 1}`,
    params: dep.jenkins_params || {},
  }))
  if (!plannedRuns.length && st.jenkins_params && Object.keys(st.jenkins_params).length) {
    plannedRuns.push({ index: 1, label: 'Deploy', params: st.jenkins_params })
  }

  // null → follow the latest attempt; a build_number → user pinned that attempt.
  const [selectedBuild, setSelectedBuild] = useState(null)
  const [retrying, setRetrying] = useState(false)

  const effectiveBuild = selectedBuild ?? (latest ? latest.build_number : null)
  const selected = attempts.find((a) => a.build_number === effectiveBuild) || latest
  const isViewingLatest = !!latest && !!selected && selected.build_number === latest.build_number

  async function handleRetry(e) {
    e.stopPropagation()
    if (retrying) return
    setRetrying(true)
    try {
      await onRetry(st.sub_task_id)
      setSelectedBuild(null) // snap back to following the newest attempt
    } catch (_) {
      /* error surfaced by parent */
    } finally {
      setRetrying(false)
    }
  }

  // Which console + report to show: the selected attempt if we have history,
  // otherwise the sub-task's live console/report (e.g. failed before any build).
  const consoleText = selected
    ? selected.console_log || (isViewingLatest ? st.console_log : '')
    : st.console_log
  const consoleBuild = selected ? selected.build_number : (st.jenkins_build_number || st.mock_build_number)
  const consoleUrl = selected ? selected.build_url : st.jenkins_build_url
  const consoleLive = isViewingLatest ? st.status === 'running' : false
  const report = selected ? selected.ai_report || (isViewingLatest ? st.ai_report : null) : st.ai_report

  function stepIcon(status) {
    if (status === 'done') return '✓'
    if (status === 'running') return '●'
    if (status === 'skipped') return '–'
    if (status === 'failed') return isAborted ? '⏹' : '✕'
    return '○'
  }

  function displayStepLabel(label) {
    if (!label) return label
    return label
      .replace(/^AI verification: Ollama reads Jenkins logs, confirms success$/i, 'Post-build verification')
      .replace(/^Waiting for Jenkins build #\d+ to complete$/i, 'Wait for Jenkins build to complete')
      .replace(/^Poll API gateway: GET .+$/i, 'Health check: poll API gateway')
      .replace(/^Poll portal URL for HTTP 200 every 5 min$/i, 'Health check: poll portal URL')
  }

  return (
    <div
      className={`op-sub-task op-status-${st.status} ${isAborted ? 'op-status-aborted' : ''} ${isExpanded ? 'op-expanded' : ''}`}
      onClick={onToggle}
    >
      <div className="op-st-header">
        <span className="op-st-icon">{SECTION_ICONS[st.section] || '🔧'}</span>
        <div className="op-st-info">
          <span className="op-st-id">{st.sub_task_id}</span>
          <span className="op-st-label">{st.label}</span>
        </div>
        <div className="op-st-right">
          <span className="op-st-agent">{st.agent}</span>
          <span className={`badge badge-${isAborted ? 'aborted' : st.status}`}>
            {isAborted ? 'stopped' : st.status}
          </span>
          {canRetry && (
            <button
              type="button"
              className="op-retry-btn"
              onClick={handleRetry}
              disabled={retrying}
              title="Re-run this build"
            >
              {retrying ? '↻ Retrying…' : '↻ Retry'}
            </button>
          )}
          <span className="op-expand-toggle">{isExpanded ? '▲' : '▼'}</span>
        </div>
      </div>

      {isExpanded && (
        <div className="op-st-steps">
          {showJobCard && (
            <div className="op-st-card">
              <div className="op-card-title">
                {preview ? 'What will run' : 'Execution details'}
              </div>
              <div className="op-card-row"><span>Agent</span><span>{st.agent}</span></div>
              {st.jenkins_job && (
                <div className="op-card-row"><span>Jenkins job</span><span>{st.jenkins_job}</span></div>
              )}
              <div className="op-card-row">
                <span>Type</span><span>{st.section} · {st.sub_type || '—'}</span>
              </div>
              {st.build_only && (
                <div className="op-card-row">
                  <span>Mode</span><span>Build only (no merge)</span>
                </div>
              )}
              {st.release_branch && (
                <div className="op-card-row"><span>Release branch</span><span>{st.release_branch}</span></div>
              )}
              {(st.jenkins_build_number || st.mock_build_number) && (
                <div className="op-card-row">
                  <span>Jenkins build</span>
                  <span>
                    {st.jenkins_build_url ? (
                      <a href={st.jenkins_build_url} target="_blank" rel="noreferrer">
                        #{st.jenkins_build_number || st.mock_build_number} ↗
                      </a>
                    ) : (
                      `#${st.jenkins_build_number || st.mock_build_number}`
                    )}
                  </span>
                </div>
              )}
              {plannedRuns.length > 1 ? (
                <div className="op-card-params">
                  <span className="op-card-params-label">
                    Jenkins runs ({plannedRuns.length} sequential)
                  </span>
                  {plannedRuns.map((run) => (
                    <div key={run.index} className="op-deploy-run">
                      <div className="op-card-row">
                        <span>Run {run.index}</span><span>{run.label}</span>
                      </div>
                      {Object.entries(run.params).map(([k, v]) => (
                        <div className="op-card-row op-card-row-nested" key={`${run.index}-${k}`}>
                          <span>{k}</span><span>{String(v) || '—'}</span>
                        </div>
                      ))}
                    </div>
                  ))}
                </div>
              ) : plannedRuns.length === 1 && Object.keys(plannedRuns[0].params).length > 0 ? (
                <div className="op-card-params">
                  <span className="op-card-params-label">Trigger parameters</span>
                  {Object.entries(plannedRuns[0].params).map(([k, v]) => (
                    <div className="op-card-row" key={k}>
                      <span>{k}</span><span>{String(v) || '—'}</span>
                    </div>
                  ))}
                </div>
              ) : st.jenkins_params && Object.keys(st.jenkins_params).length > 0 ? (
                <div className="op-card-params">
                  <span className="op-card-params-label">Trigger parameters</span>
                  {Object.entries(st.jenkins_params).map(([k, v]) => (
                    <div className="op-card-row" key={k}>
                      <span>{k}</span><span>{String(v) || '—'}</span>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          )}

          <div className="op-steps-label">{preview ? 'Steps that will execute' : 'Steps'}</div>
          {st.steps.map((step) => {
            const abortedStep = step.status === 'failed' && isAborted
            return (
              <div key={step.step_id} className={`op-step op-step-${step.status}${abortedStep ? ' op-step-aborted' : ''}`}>
                <span className="op-step-icon">{stepIcon(step.status)}</span>
                <span className="op-step-label">
                  {displayStepLabel(step.label)}
                  {showStepDetail && step.detail && (
                    <span className="op-step-detail">{step.detail}</span>
                  )}
                </span>
                {step.ts && <span className="op-step-ts">{formatTs(step.ts)}</span>}
              </div>
            )
          })}

          {/* Build history: chips show each attempt's outcome (last vs current). */}
          {showParamsAndLogs && attempts.length > 0 && (
            <div className="op-builds" onClick={(e) => e.stopPropagation()}>
              <div className="op-steps-label" style={{ marginBottom: 6 }}>
                Build history {attempts.length > 1 ? `· ${attempts.length} attempts` : ''}
              </div>
              <div className="op-builds-chips">
                {attempts.map((a) => {
                  const kind = attemptKind(a)
                  const isSel = a.build_number === effectiveBuild
                  return (
                    <button
                      key={a.build_number}
                      type="button"
                      className={`op-build-chip op-build-${kind} ${isSel ? 'is-selected' : ''}`}
                      onClick={() => setSelectedBuild(a.build_number)}
                    >
                      <span className="op-build-chip-dot" />
                      <span className="op-build-chip-num">
                        #{a.attempt}{a.build_number ? ` · ${a.build_number}` : ''}
                        {a.build_number === latest?.build_number ? ' (current)' : ''}
                      </span>
                      <span className="op-build-chip-state">{attemptStateLabel(a)}</span>
                    </button>
                  )
                })}
              </div>

              {report && <AiReportCard report={report} retryCount={st.retry_count} />}

              {consoleText ? (
                <JenkinsConsole
                  text={consoleText}
                  live={consoleLive}
                  buildNumber={consoleBuild}
                  url={consoleUrl}
                />
              ) : (
                <div className="op-console-empty">No console output captured for this build.</div>
              )}
            </div>
          )}

          {/* Fallback for failures that never produced a build (e.g. cancelled). */}
          {showParamsAndLogs && attempts.length === 0 && report && (
            <AiReportCard report={report} retryCount={st.retry_count} />
          )}
          {showParamsAndLogs && attempts.length === 0 && st.console_log && (
            <JenkinsConsole
              text={st.console_log}
              live={st.status === 'running'}
              buildNumber={st.jenkins_build_number || st.mock_build_number}
              url={st.jenkins_build_url}
            />
          )}

          {/* AI report for non-DevOps reviewers (no logs/console for them). */}
          {showStepDetail && !showParamsAndLogs && st.ai_report && (
            <AiReportCard report={st.ai_report} retryCount={st.retry_count} />
          )}

          {showParamsAndLogs && st.logs && st.logs.length > 0 && (
            <ActivityLog lines={st.logs} />
          )}
        </div>
      )}
    </div>
  )
}

/** Map an attempt to a colour bucket for its chip. */
function attemptKind(a) {
  if (!a) return 'default'
  if (a.status === 'running') return 'running'
  const cat = a.category || ''
  if (cat === 'success' || a.status === 'done') return 'success'
  if (cat === 'user_aborted') return 'aborted'
  return 'error'
}

function attemptStateLabel(a) {
  if (!a) return ''
  if (a.status === 'running') return 'running'
  if (a.category === 'user_aborted') return 'stopped'
  if (a.category === 'success' || a.status === 'done') return 'success'
  return a.category || a.status || 'failed'
}

/** Auto-scroll to the bottom on new content, but only if the user is already
 *  near the bottom — so scrolling up to read older output isn't yanked back. */
function useStickyScroll(dep) {
  const ref = useRef(null)
  const stickRef = useRef(true)
  const onScroll = useCallback(() => {
    const el = ref.current
    if (!el) return
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48
  }, [])
  useEffect(() => {
    const el = ref.current
    if (el && stickRef.current) el.scrollTop = el.scrollHeight
  }, [dep])
  return { ref, onScroll }
}

/** Classify a log message so the activity feed can colour-code each line. */
function classifyLogLine(msg) {
  const m = msg.toLowerCase()
  if (/manual retry|── manual retry/.test(m)) return 'retry'
  if (/failed|error|crashed|✕/.test(m)) return 'error'
  if (/aborted|cancelled|canceled|stopped|⏹/.test(m)) return 'aborted'
  if (/succeeded|success|healthy|complete|http 200|deployment complete|✓/.test(m)) return 'success'
  if (/queued|waiting/.test(m)) return 'queued'
  if (/auto-retry|retry \d/.test(m)) return 'retry'
  if (/triggered|dispatched|slot acquired|context loaded|built jenkins params|running/.test(m)) return 'info'
  return 'default'
}

const LOG_KIND_ICON = {
  error: '✕',
  aborted: '⏹',
  success: '✓',
  queued: '⏳',
  retry: '↻',
  info: '›',
  default: '·',
}

function ActivityLog({ lines }) {
  const { ref, onScroll } = useStickyScroll(lines.length)

  return (
    <div className="op-activity" onClick={(e) => e.stopPropagation()}>
      <div className="op-steps-label" style={{ marginBottom: 6 }}>Orchestrator activity</div>
      <div ref={ref} onScroll={onScroll} className="op-activity-log">
        {lines.map((line, i) => {
          const match = line.match(/^\[([^\]]+)\]\s?(.*)$/)
          const ts = match ? match[1] : ''
          const msg = match ? match[2] : line
          const kind = classifyLogLine(msg)
          return (
            <div key={i} className={`op-log-line op-log-${kind}`}>
              <span className="op-log-icon">{LOG_KIND_ICON[kind]}</span>
              {ts && <span className="op-log-ts">{formatTs(ts)}</span>}
              <span className="op-log-msg">{msg}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function formatTs(ts) {
  // Show just HH:MM:SS from an ISO-ish timestamp; fall back to the raw value.
  const m = String(ts).match(/(\d{2}:\d{2}:\d{2})/)
  return m ? m[1] : ts
}

function JenkinsConsole({ text, live, buildNumber, url }) {
  const { ref, onScroll } = useStickyScroll(text)

  return (
    <div className="op-console" style={{ marginTop: 10 }} onClick={(e) => e.stopPropagation()}>
      <div className="op-console-header">
        <span className="op-console-title">
          {live && <span className="op-console-live">● LIVE</span>} Jenkins console
          {buildNumber ? ` — #${buildNumber}` : ''}
        </span>
        {url && (
          <a href={url} target="_blank" rel="noreferrer" className="op-console-link">
            Open in Jenkins ↗
          </a>
        )}
      </div>
      <pre ref={ref} onScroll={onScroll} className="op-console-body">{text}</pre>
    </div>
  )
}

function AiReportCard({ report, retryCount }) {
  if (!report) return null
  const isSuccess = report.category === 'success'
  const isAborted = report.category === 'user_aborted'
  const kind = isSuccess ? 'success' : isAborted ? 'aborted' : report.retryable ? 'retry' : 'error'
  const title = isSuccess
    ? 'AI verification'
    : isAborted
      ? 'Build manually stopped'
      : `AI failure analysis — ${report.category}${report.retryable ? ' (auto-retry)' : ''}`
  const icon = isSuccess ? '✅' : isAborted ? '⏹' : '🤖'

  return (
    <div className={`op-ai-report op-ai-${kind}`}>
      <div className="op-ai-title">
        <span className="op-ai-icon">{icon}</span>
        <span>{title}</span>
        {report.ai_available === false && !isSuccess && !isAborted && (
          <span className="op-ai-tag">rule-based · Ollama offline</span>
        )}
      </div>
      {report.summary && <div className="op-ai-summary">{report.summary}</div>}
      {report.root_cause && (
        <div className="op-ai-cause"><strong>Root cause:</strong> {report.root_cause}</div>
      )}
      {Array.isArray(report.remediation_steps) && report.remediation_steps.length > 0 && (
        <div className="op-ai-remediation">
          <strong>Remediation</strong>
          <ol>
            {report.remediation_steps.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ol>
        </div>
      )}
      <div className="op-ai-meta">
        {retryCount ? `Attempts: ${retryCount + 1} · ` : ''}
        {typeof report.confidence === 'number' ? `Confidence: ${report.confidence}%` : ''}
      </div>
    </div>
  )
}

export { SECTION_ICONS }
