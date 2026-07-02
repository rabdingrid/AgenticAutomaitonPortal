import React, { useState } from 'react'

const SECTION_ICONS = { build: '🔨', yaml: '📄', db: '🗄️', phrases: '💬' }

/**
 * OrchestratorPlan
 *
 * @param plan      task.orchestrator_plan  ({ phases, total_sub_tasks, estimated_minutes })
 * @param subTasks  flat list of sub-task objects
 * @param role      current user's role — controls detail level:
 *                   developer → status only (no step detail / params / logs)
 *                   dev_lead/qa → phase layout + step detail (no params / logs)
 *                   devops → full detail (steps, detail text, Jenkins params, logs)
 */
export default function OrchestratorPlan({ plan, subTasks, role = 'developer', preview = false }) {
  const [expanded, setExpanded] = useState(null)

  if (!plan || !plan.phases) return null

  const showStepDetail = role !== 'developer'
  const showParamsAndLogs = role === 'devops'
  // The "what will run" details card (job, agent, params) is a DevOps affordance.
  const showJobCard = role === 'devops'
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
              const isExpanded = expanded === stId

              return (
                <div
                  key={stId}
                  className={`op-sub-task op-status-${st.status} ${isExpanded ? 'op-expanded' : ''}`}
                  onClick={() => setExpanded(isExpanded ? null : stId)}
                >
                  <div className="op-st-header">
                    <span className="op-st-icon">{SECTION_ICONS[st.section] || '🔧'}</span>
                    <div className="op-st-info">
                      <span className="op-st-id">{stId}</span>
                      <span className="op-st-label">{st.label}</span>
                    </div>
                    <div className="op-st-right">
                      <span className="op-st-agent">{st.agent}</span>
                      <span className={`badge badge-${st.status}`}>{st.status}</span>
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
                          {st.release_branch && (
                            <div className="op-card-row"><span>Release branch</span><span>{st.release_branch}</span></div>
                          )}
                          {st.mock_build_number && (
                            <div className="op-card-row"><span>Jenkins build</span><span>#{st.mock_build_number}</span></div>
                          )}
                          {st.jenkins_params && Object.keys(st.jenkins_params).length > 0 && (
                            <div className="op-card-params">
                              <span className="op-card-params-label">Trigger parameters</span>
                              {Object.entries(st.jenkins_params).map(([k, v]) => (
                                <div className="op-card-row" key={k}>
                                  <span>{k}</span><span>{String(v) || '—'}</span>
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      )}

                      <div className="op-steps-label">{preview ? 'Steps that will execute' : 'Steps'}</div>
                      {st.steps.map((step) => (
                        <div key={step.step_id} className={`op-step op-step-${step.status}`}>
                          <span className="op-step-icon">
                            {step.status === 'done' ? '✓' : step.status === 'running' ? '●' : step.status === 'failed' ? '✕' : '○'}
                          </span>
                          <span className="op-step-label">
                            {step.label}
                            {showStepDetail && step.detail && (
                              <span className="op-step-detail">{step.detail}</span>
                            )}
                          </span>
                          {step.ts && <span className="op-step-ts">{step.ts}</span>}
                        </div>
                      ))}

                      {showParamsAndLogs && st.logs && st.logs.length > 0 && (
                        <div className="log-box" style={{ maxHeight: 100, marginTop: 8 }}>
                          {st.logs.join('\n')}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      ))}
    </div>
  )
}

export { SECTION_ICONS }
