# Deployment Portal — v4 Implementation Spec
# For Cursor AI — Read every section before writing any code

## CRITICAL: Read This First

You are building on top of an existing working portal (v2/v3 spec
already partially implemented). Do NOT rewrite files that don't need
changes. Every section below is clearly marked:
- `(NEW FILE)` — create from scratch
- `(MODIFY)` — change only the listed parts of an existing file
- `(NO CHANGE)` — do not touch

The two biggest changes in this spec are:
1. **Sub-task model** — a Job is now a container for Sub-Tasks. Each
   sub-task is one atomic unit of work (one merge, one Jenkins build,
   one YAML file, one DB script). The orchestrator plans and executes
   sub-tasks, not jobs.
2. **Validation before submit** — the Validate button calls real
   mock services and shows specific per-field errors. Submit is
   disabled until all validations pass.

All dummy/mock data must be realistic and deterministic (same input →
same output) so demos are repeatable and don't randomly change between
runs.

---

## 1. Data Model Changes (backend/db.py — MODIFY)

### 1.1 New: Sub-Task concept

Currently: Task → Jobs (one job per section: build, yaml, db, phrases)

New: Task → Jobs → Sub-Tasks

A Job is a section container. Sub-Tasks are the real atomic units.
Examples:
- Build job with 2 microservices selected → 2 sub-tasks:
  "Merge & Build AccountService !2814" and "Merge & Build BillingService !2819"
- YAML job with 3 files → 3 sub-tasks:
  "Apply parameters.yml", "Apply billing-rules.yml", "Apply config.yml"
- DB job → 1 sub-task per script
- Phrases job → 1 sub-task per service selected

Each sub-task has:
```python
{
  "sub_task_id": "ST-1",           # unique across the whole task
  "job_id": "JOB-2",               # parent job
  "task_id": "TASK-20392",
  "section": "build",              # build | yaml | db | phrases
  "sub_type": "microservice",      # microservice | portal | utility
  "service_key": "account-service",
  "label": "AccountService",
  "release_branch": "release/2026-07",   # null for build
  "order": 1,                       # global order across ALL sub-tasks in the task
  "status": "queued",               # queued | running | done | failed
  "agent": "merge_build_agent",     # which agent handles this sub-task
  "steps": [                        # the internal steps THIS sub-task goes through
    {"step_id": "s1", "label": "Validate MR is mergeable", "status": "queued", "detail": null, "ts": null},
    {"step_id": "s2", "label": "Merge !2814 into release branch", "status": "queued", "detail": null, "ts": null},
    {"step_id": "s3", "label": "Trigger Jenkins Titan-Microservices", "status": "queued", "detail": null, "ts": null},
    {"step_id": "s4", "label": "Waiting for Jenkins build #1284", "status": "queued", "detail": null, "ts": null},
    {"step_id": "s5", "label": "Poll API gateway for service health", "status": "queued", "detail": null, "ts": null},
    {"step_id": "s6", "label": "AI verification of Jenkins logs", "status": "queued", "detail": null, "ts": null}
  ],
  "logs": [],
  "jenkins_job": "Titan-Microservices",
  "jenkins_params": {"Service": "AccountService", "MergeID": "!2814", ...},
  "mock_build_number": null,        # populated when Jenkins step starts
  "created_at": "...",
  "updated_at": "..."
}
```

### 1.2 Sub-task step templates per section

These are the EXACT steps to show in each sub-task type.
Do not invent different steps — use these verbatim.

**Build sub-task (microservice):**
```
1. Validate MR is mergeable (GitSpace check)
2. Merge !{mr_id} into {release_branch}
3. Trigger Jenkins job: {jenkins_job}
4. Waiting for Jenkins build #{build_number} to complete
5. Poll API gateway: GET {gateway_url}/health every 5 min (mock: 3 polls)
6. AI verification: Ollama reads Jenkins logs, confirms success
```

**Build sub-task (portal):**
```
1. Validate MR is mergeable (GitSpace check)
2. Merge !{mr_id} into {release_branch}
3. Trigger Jenkins job: Titan-Portals
4. Waiting for Jenkins build #{build_number} to complete
5. Poll portal URL for HTTP 200 every 5 min (mock: 3 polls)
6. AI verification: Ollama reads Jenkins logs, confirms success
```

**YAML sub-task:**
```
1. Validate YAML syntax (parse check)
2. Validate config file against schema
3. Trigger Jenkins job: yml_automation_2.0
4. Waiting for Jenkins build #{build_number} to complete
5. Verify config applied: GET {config_endpoint} (mock)
6. Mark config propagated
```

**DB sub-task:**
```
1. Validate SQL syntax (no DROP/TRUNCATE/DELETE without WHERE)
2. Validate Liquibase changeset format
3. Run Liquibase update (mock)
4. Verify DB migration applied
5. Run smoke test query
```

**Phrases sub-task:**
```
1. Validate phrase file format
2. Validate phrases against key schema
3. Trigger phrase deployment job
4. Verify phrases propagated to CDN (mock)
```

### 1.3 Orchestrator execution plan

The orchestrator determines sub-task execution order. This is
computed when the task is CREATED and stored as
`task["orchestrator_plan"]`.

**Ordering rules:**
```
Phase 1 (run first, can run in parallel with each other):
  ALL DB sub-tasks

Phase 2 (run after all Phase 1 complete, can run in parallel with each other):
  ALL YAML sub-tasks
  ALL Phrases sub-tasks

Phase 3 (run after Phase 2 complete, can run in parallel with each other):
  ALL Build sub-tasks (each is one merge + jenkins job)
```

**Why this order:** DB migrations must run before new code deploys.
YAML/Phrases config must be in place before services start using it.
Build merges happen last so the deployed code finds the DB and config
already in the new state.

**Within a phase, sub-tasks of the same type run in the order they
were added in the form (order they appear in `links[]`).**

**Orchestrator plan shape stored on task:**
```python
task["orchestrator_plan"] = {
  "phases": [
    {
      "phase": 1,
      "label": "Database migrations",
      "parallel": True,
      "sub_task_ids": ["ST-3"]
    },
    {
      "phase": 2,
      "label": "Config & phrases",
      "parallel": True,
      "sub_task_ids": ["ST-2", "ST-4", "ST-5"]
    },
    {
      "phase": 3,
      "label": "Build & deploy",
      "parallel": True,
      "sub_task_ids": ["ST-1", "ST-6"]
    }
  ],
  "total_sub_tasks": 6,
  "estimated_minutes": 45
}
```

### 1.4 Updated db.py functions

**`create_task()` changes:**
- Now creates Sub-Tasks (not just Jobs) from `sections_payload`
- Each link in a section becomes ONE sub-task
- Computes and stores `orchestrator_plan`
- Global sub-task counter added to DB: `"counters": {"task": ..., "job": ..., "sub_task": 0}`
- All sub-tasks start as `"queued"` — orchestrator starts them after approval
- `task["sub_tasks"]` = list of all sub-task IDs for this task (flat)

**New functions to add:**
```python
def get_sub_task(sub_task_id: str) -> dict | None
def update_sub_task_status(sub_task_id: str, status: str, log_line: str | None = None) -> dict | None
def advance_sub_task_step(sub_task_id: str, step_id: str, status: str, detail: str | None = None) -> dict | None
def get_task_with_sub_tasks(task_id: str) -> dict | None  # returns task + all sub-tasks expanded
def mock_tick_sub_task(sub_task_id: str) -> dict  # advance one step in the mock simulation
def get_orchestrator_plan(task_id: str) -> dict | None
```

**`apply_approval_decision()` changes:**
When the final approval is given and task moves to `running`:
- Find sub-tasks in Phase 1 of the orchestrator plan
- Set those sub-tasks' status to `"running"` (not the jobs)
- Advance each running sub-task to step 1 (first step → status "running")
- Leave all other sub-tasks as `"queued"`

---

## 2. Mock Execution Engine (backend/mock_executor.py — NEW FILE)

This is the most important new backend file. It simulates what real
Jenkins/GitLab/API-gateway calls would do, so the UI shows realistic
progress without real integrations.

```python
"""
mock_executor.py

Simulates real execution: merge, Jenkins build, health polling, AI verification.
Every function here has the same signature it will have when backed by
real integrations. Swap internals only — callers never change.

Called by:
  - POST /sub-tasks/{id}/tick  (frontend polls this while a sub-task is running)
  - Background simulation loop (optional — see Section 7)
"""

import random
import time
from datetime import datetime, timezone

# Mock configuration — realistic timings and outcomes
MOCK_BUILD_DURATION_STEPS = 4       # number of tick() calls before build "completes"
MOCK_HEALTH_POLL_ATTEMPTS = 3       # number of polls before service "up"
MOCK_FAILURE_RATE = 0.08            # 8% of sub-tasks fail on a random step (for demo realism)
MOCK_JENKINS_BASE_BUILD_NUMBER = 1280


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def tick_sub_task(sub_task: dict) -> dict:
    """
    Called on each poll. Advances the sub-task one step forward.
    Returns {"step_advanced": bool, "new_step_status": str, "completed": bool, "failed": bool, "detail": str}

    This is the ONLY function the frontend needs to call to simulate progress.
    Never call step-specific functions directly from frontend.
    """
    steps = sub_task["steps"]
    current_step = next((s for s in steps if s["status"] == "running"), None)
    next_step = next((s for s in steps if s["status"] == "queued"), None)

    # Nothing running yet — start first step
    if current_step is None and next_step is not None:
        next_step["status"] = "running"
        next_step["ts"] = _now()
        return {"step_advanced": True, "new_step_status": "running",
                "completed": False, "failed": False,
                "detail": f"Started: {next_step['label']}"}

    if current_step is None:
        # All steps done
        return {"step_advanced": False, "new_step_status": "done",
                "completed": True, "failed": False, "detail": "All steps complete"}

    # Simulate step completion — some steps take multiple ticks
    section = sub_task["section"]
    step_idx = next(i for i, s in enumerate(steps) if s["step_id"] == current_step["step_id"])

    # Inject realistic detail into the step
    detail = _mock_step_detail(sub_task, current_step, step_idx)

    # Mock failure injection (only on non-first, non-last steps for demo safety)
    if MOCK_FAILURE_RATE > 0 and 0 < step_idx < len(steps) - 1:
        seed = sum(ord(c) for c in sub_task["sub_task_id"] + current_step["step_id"])
        if random.Random(seed).random() < MOCK_FAILURE_RATE:
            current_step["status"] = "failed"
            current_step["detail"] = f"MOCK ERROR: {detail}"
            current_step["ts"] = _now()
            return {"step_advanced": True, "new_step_status": "failed",
                    "completed": False, "failed": True,
                    "detail": current_step["detail"]}

    # Complete current step, advance to next
    current_step["status"] = "done"
    current_step["detail"] = detail
    current_step["ts"] = _now()

    if next_step:
        next_step["status"] = "running"
        next_step["ts"] = _now()
        return {"step_advanced": True, "new_step_status": "running",
                "completed": False, "failed": False, "detail": detail}
    else:
        return {"step_advanced": True, "new_step_status": "done",
                "completed": True, "failed": False, "detail": "Sub-task complete"}


def _mock_step_detail(sub_task: dict, step: dict, step_idx: int) -> str:
    """Generate realistic detail text for each step type."""
    label = sub_task.get("label", "service")
    service_key = sub_task.get("service_key", "service")
    build_num = sub_task.get("mock_build_number") or (MOCK_JENKINS_BASE_BUILD_NUMBER + hash(service_key) % 100)
    section = sub_task["section"]

    if section == "build":
        details = [
            f"GitSpace API: MR is approved and pipeline green ✓",
            f"Merged into release/2026-07 — commit sha: a3f{hash(service_key) % 10000:04x}",
            f"Jenkins #{build_num} triggered — job: {sub_task.get('jenkins_job', 'Titan-Microservices')}",
            f"Jenkins #{build_num}: tests passed (142/142), Docker image built, pushed to ECR",
            f"API gateway /health → HTTP 200 — service responding (attempt 2/3)",
            f"Ollama verified: 'Build #{build_num} succeeded. No errors in deployment logs. Service healthy.'"
        ]
    elif section == "yaml":
        details = [
            f"YAML syntax valid — no parse errors in {label}",
            f"Schema validation passed — all required keys present",
            f"Jenkins yml_automation_2.0 #{build_num} triggered",
            f"Build #{build_num} complete — config applied",
            f"Config endpoint returned expected values ✓",
            f"Config propagation confirmed across all instances"
        ]
    elif section == "db":
        details = [
            f"SQL syntax valid — no DROP/TRUNCATE/DELETE without WHERE detected",
            f"Liquibase changeset format valid — 3 changesets found",
            f"Liquibase update: 3/3 changesets applied successfully",
            f"DB migration verified — schema matches expected state",
            f"Smoke query returned expected result set ✓"
        ]
    elif section == "phrases":
        details = [
            f"Phrase file format valid — 47 keys found",
            f"All keys present in master schema",
            f"Phrase deployment job triggered",
            f"CDN cache invalidated — phrases propagated to 3/3 regions ✓"
        ]
    else:
        details = [f"Step {step_idx + 1} complete"]

    return details[step_idx] if step_idx < len(details) else "Step complete"


def mock_validate_section(section: str, links: list[dict]) -> list[dict]:
    """
    Called by POST /tasks/validate to return per-link validation results.
    Returns a list of {"service_key": str, "label": str, "valid": bool, "errors": list[str]}

    Real version would call GitSpace API, YAML parser, SQL linter, etc.
    Mock version simulates realistic validation errors for demo purposes.
    """
    results = []
    for link in links:
        service_key = link.get("service_key", "")
        label = link.get("label", service_key)
        errors = []

        if not service_key:
            errors.append("No service selected")
        else:
            seed = sum(ord(c) for c in service_key + section)
            rng = random.Random(seed)

            if section == "build":
                # 10% chance of "MR has conflicts" for realistic demo
                if rng.random() < 0.10:
                    errors.append(f"MR !{2800 + rng.randint(1, 99)}: merge conflicts detected — resolve before submitting")
                # 5% chance of "pipeline not green"
                elif rng.random() < 0.05:
                    errors.append(f"GitSpace pipeline not green for {label} — 2 tests failing")

            elif section == "yaml":
                if rng.random() < 0.08:
                    errors.append(f"YAML syntax error in {label}: unexpected token at line 14")

            elif section == "db":
                if rng.random() < 0.12:
                    errors.append(f"SQL risk detected in {label}: DELETE statement without WHERE clause")

            elif section == "phrases":
                if rng.random() < 0.06:
                    errors.append(f"Missing required phrase key 'checkout.error.retry' in {label}")

        results.append({
            "service_key": service_key,
            "label": label,
            "section": section,
            "valid": len(errors) == 0,
            "errors": errors
        })

    return results
```

---

## 3. API Endpoint Changes (backend/main.py — MODIFY)

### 3.1 New endpoints to add

```python
# --- Validation endpoint (already in v3 spec, enhance to use mock_executor) ---
@app.post("/tasks/validate")
def validate_request(payload: ValidateRequest) -> dict:
    """
    Per-link validation using mock_executor.mock_validate_section().
    Returns {"valid": bool, "sections": [{section, results: [{service_key, label, valid, errors}]}], "summary_errors": [str]}
    Never raises — always returns 200 with valid/invalid payload.
    """
    from mock_executor import mock_validate_section
    all_valid = True
    section_results = []
    summary_errors = []

    for sec in payload.sections:
        link_results = mock_validate_section(sec.section, [l.model_dump() for l in sec.links])
        section_ok = all(r["valid"] for r in link_results)
        if not section_ok:
            all_valid = False
            for r in link_results:
                for err in r["errors"]:
                    summary_errors.append(f"[{sec.section.upper()}] {err}")
        section_results.append({"section": sec.section, "results": link_results})

    if not payload.environment:
        all_valid = False
        summary_errors.append("Environment is required")
    if not payload.jira_id.strip():
        all_valid = False
        summary_errors.append("Jira ID is required")
    if not payload.sections:
        all_valid = False
        summary_errors.append("At least one section must be filled")

    return {"valid": all_valid, "sections": section_results, "summary_errors": summary_errors}


# --- Sub-task endpoints ---
@app.get("/sub-tasks/{sub_task_id}")
def get_sub_task(sub_task_id: str) -> dict:
    st = db.get_sub_task(sub_task_id)
    if not st:
        raise HTTPException(404, f"Sub-task {sub_task_id} not found")
    return st


@app.post("/sub-tasks/{sub_task_id}/tick")
def tick_sub_task(sub_task_id: str) -> dict:
    """
    Advance the sub-task one step in the mock simulation.
    Frontend calls this every few seconds while sub-task is running.
    Returns the updated sub-task with tick result.
    """
    st = db.get_sub_task(sub_task_id)
    if not st:
        raise HTTPException(404, detail=f"Sub-task {sub_task_id} not found")
    if st["status"] not in ("running", "queued"):
        return st  # already done or failed, no-op

    from mock_executor import tick_sub_task as _tick
    tick_result = _tick(st)

    # Persist updated steps
    new_status = st["status"]
    if tick_result["completed"]:
        new_status = "done"
    elif tick_result["failed"]:
        new_status = "failed"
    elif st["status"] == "queued":
        new_status = "running"

    updated = db.update_sub_task_after_tick(sub_task_id, st["steps"], new_status, tick_result["detail"])
    if tick_result["completed"] or tick_result["failed"]:
        db.advance_orchestrator_phase(st["task_id"])

    return {"sub_task": updated, "tick": tick_result}


@app.get("/tasks/{task_id}/orchestrator-plan")
def get_orchestrator_plan(task_id: str) -> dict:
    plan = db.get_orchestrator_plan(task_id)
    if not plan:
        raise HTTPException(404, detail=f"Task {task_id} not found")
    return plan


@app.get("/tasks/{task_id}/full")
def get_task_full(task_id: str) -> dict:
    """Returns task + all jobs + all sub-tasks expanded. Used by TaskDetail page."""
    task = db.get_task_with_sub_tasks(task_id)
    if not task:
        raise HTTPException(404, detail=f"Task {task_id} not found")
    return task
```

---

## 4. Frontend: Validation Flow (frontend/src/pages/NewRequest.jsx — MODIFY)

### 4.1 Validate button behavior

The Validate button is the **only path to enabling Submit**. Sequence:

```
User fills form
    ↓
User clicks VALIDATE
    ↓
POST /tasks/validate with full payload
    ↓ Loading state on button ("Validating...")
    ↓
Response arrives
    ↓
If valid=false:
    Show per-section, per-link error list (NOT a single alert box)
    Each link that failed shows a red ✕ badge next to it in the form
    Each valid link shows a green ✓ badge
    Submit button stays DISABLED
    Error message under each failing link shows the exact error text
    ↓
If valid=true:
    Show "All checks passed ✓" in green
    Each link shows green ✓
    Submit button becomes ENABLED
    ↓
If user changes ANY field after validation:
    Reset all validation badges to neutral
    Hide "All checks passed" message
    Disable Submit again
    Force re-validate
```

### 4.2 Validation UI component structure

```jsx
// After each link row in LinkSectionEditor, show validation badge:
<div className="link-row" key={i}>
  <select .../>   {/* sub_type */}
  <select .../>   {/* service_key */}
  <div className={`validation-badge ${getValidationState(section, link.service_key)}`}>
    {getValidationState(section, link.service_key) === 'valid' && '✓'}
    {getValidationState(section, link.service_key) === 'invalid' && '✕'}
    {getValidationState(section, link.service_key) === 'pending' && '–'}
  </div>
  <button className="link-remove-btn">✕</button>
</div>
{getValidationErrors(section, link.service_key).map(err => (
  <p className="validation-error-text" key={err}>{err}</p>
))}
```

CSS classes to add to styles.css:
```css
.validation-badge {
  width: 24px; height: 24px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 13px; font-weight: 700; flex-shrink: 0;
}
.validation-badge.valid { background: var(--green-light); color: var(--green); }
.validation-badge.invalid { background: var(--red-light); color: var(--red); }
.validation-badge.pending { background: var(--slate-light); color: var(--text-tertiary); }
.validation-error-text { font-size: 11.5px; color: var(--red); margin: 2px 0 8px 0; }
```

---

## 5. Frontend: Role-Based Views

### 5.1 Role switcher (temporary until real auth)

Add a role switcher in the sidebar — a simple dropdown with 4 roles.
This controls what each page shows. Store in React context.

```jsx
// In App.jsx, wrap everything in RoleContext
const RoleContext = React.createContext('developer')

// In sidebar, add:
<select
  value={role}
  onChange={e => setRole(e.target.value)}
  style={{ width: '100%', marginTop: 'auto', fontSize: 12 }}
>
  <option value="developer">Developer</option>
  <option value="dev_lead">Dev Lead</option>
  <option value="qa">QA</option>
  <option value="devops">DevOps</option>
</select>
```

### 5.2 What each role sees — precise rules

#### Developer view
- Can create new requests (New Request page fully visible)
- History: sees only their own tasks (filter by `requested_by="demo-user"`)
- Task Detail: sees request details, current approval status, read-only orchestrator view (no approve buttons)
- Cannot see Code Freeze toggle
- Cannot see Orchestrator Plan detail (sub-task steps visible, but no "run" controls)

#### Dev Lead view
- History: sees ALL tasks, filtered to show "pending_approval" prominently at top
- Task Detail: sees request details + APPROVAL PANEL (Approve/Reject with comment)
- The approval panel shows ONLY when `task.approval_chain[0] === "dev_lead"` and dev_lead hasn't acted yet
- After approving/rejecting: sees their decision but not the next stage's approval UI
- Cannot see Orchestrator Plan detail (only the plan summary diagram)
- Cannot see Code Freeze toggle

#### QA view (only relevant when code freeze is ON)
- Same as Dev Lead but approval panel appears after dev_lead has approved
- If code freeze is OFF: QA sees all tasks read-only, no approval panel at all
- Can see a read-only orchestrator plan summary

#### DevOps view
- Sees ALL tasks across all users
- Final approval stage: approval panel when it's devops's turn
- FULL orchestrator plan view (see Section 6 below) — this is the most detailed view
- Code Freeze toggle on Home page
- Can see sub-task step details and logs
- History: additional columns showing "approved by" chain and timing

---

## 6. Frontend: Orchestrator Plan View (frontend/src/components/OrchestratorPlan.jsx — NEW FILE)

This is the most important new UI component. It shows the execution
plan as a visual flow diagram with phases, parallel lanes, and
sub-task detail on hover/click.

### 6.1 Visual structure

```
[Phase 1: Database migrations]
    │
    └── [ST-3: DB — billing migration] ──── DB Agent
                    ↓
[Phase 2: Config & Phrases] ← runs after Phase 1 complete
    │
    ├── [ST-2: YAML — parameters.yml] ──── YAML Agent
    ├── [ST-4: YAML — billing-rules.yml] ── YAML Agent
    └── [ST-5: Phrases — AccountService] ── Phrases Agent
                    ↓
[Phase 3: Build & Deploy] ← runs after Phase 2 complete
    │
    ├── [ST-1: Build — AccountService !2814] ── Merge-Build Agent
    └── [ST-6: Build — Admin Portal !2815] ──── Merge-Build Agent

Estimated total: ~45 minutes
```

Each sub-task box shows:
- Sub-task ID (ST-N)
- Section icon (🔨 build, 📄 yaml, 🗄️ db, 💬 phrases)
- Label (service name / file name)
- Agent assigned
- Status badge (queued/running/done/failed)
- Click → expand inline to show step list

### 6.2 OrchestratorPlan.jsx component spec

```jsx
import React, { useState } from 'react'

export default function OrchestratorPlan({ plan, subTasks, role }) {
  const [expanded, setExpanded] = useState(null)

  // plan = task.orchestrator_plan
  // subTasks = flat list of all sub_task objects
  // role = current user's role (controls detail level)

  const stMap = Object.fromEntries(subTasks.map(st => [st.sub_task_id, st]))

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
          {pi > 0 && <div className="op-phase-arrow">↓ after phase {pi} completes</div>}

          <div className="op-phase-header">
            <span className="op-phase-num">Phase {phase.phase}</span>
            <span className="op-phase-label">{phase.label}</span>
            {phase.parallel && phase.sub_task_ids.length > 1 && (
              <span className="op-parallel-badge">⟂ parallel</span>
            )}
          </div>

          <div className={`op-phase-lanes ${phase.parallel ? 'parallel' : 'sequential'}`}>
            {phase.sub_task_ids.map(stId => {
              const st = stMap[stId]
              if (!st) return null
              const isExpanded = expanded === stId

              return (
                <div
                  key={stId}
                  className={`op-sub-task op-status-${st.status}`}
                  onClick={() => setExpanded(isExpanded ? null : stId)}
                >
                  <div className="op-st-header">
                    <span className="op-st-icon">{SECTION_ICONS[st.section]}</span>
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
                      {st.steps.map(step => (
                        <div key={step.step_id} className={`op-step op-step-${step.status}`}>
                          <span className="op-step-icon">
                            {step.status === 'done' ? '✓' : step.status === 'running' ? '●' : step.status === 'failed' ? '✕' : '○'}
                          </span>
                          <span className="op-step-label">{step.label}</span>
                          {step.detail && <span className="op-step-detail">{step.detail}</span>}
                          {step.ts && <span className="op-step-ts">{step.ts}</span>}
                        </div>
                      ))}

                      {/* DevOps only: show Jenkins params */}
                      {role === 'devops' && st.jenkins_params && (
                        <div className="op-jenkins-params">
                          <p className="op-params-label">Jenkins parameters</p>
                          <div className="log-box" style={{ maxHeight: 120 }}>
                            {JSON.stringify(st.jenkins_params, null, 2)}
                          </div>
                        </div>
                      )}

                      {/* Show logs if any */}
                      {st.logs.length > 0 && (
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

const SECTION_ICONS = {
  build: '🔨', yaml: '📄', db: '🗄️', phrases: '💬'
}
```

### 6.3 Orchestrator Plan CSS (add to styles.css)

```css
.orchestrator-plan { margin-top: 1rem; }

.op-header {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 1rem;
}
.op-title { font-size: 15px; font-weight: 600; margin: 0; }
.op-meta { font-size: 12px; color: var(--text-tertiary); }

.op-phase { margin-bottom: 12px; }
.op-phase-arrow {
  text-align: center; font-size: 11.5px; color: var(--text-tertiary);
  padding: 6px 0; letter-spacing: 0.02em;
}
.op-phase-header {
  display: flex; align-items: center; gap: 10px;
  margin-bottom: 8px;
}
.op-phase-num {
  font-size: 10.5px; font-weight: 700; color: var(--white);
  background: var(--blue); padding: 2px 8px; border-radius: 4px;
}
.op-phase-label { font-size: 13px; font-weight: 600; color: var(--navy); }
.op-parallel-badge {
  font-size: 10.5px; color: var(--purple);
  background: var(--purple-light); padding: 2px 8px; border-radius: 4px;
}

.op-phase-lanes {
  display: flex; flex-direction: column; gap: 8px;
}
.op-phase-lanes.parallel {
  /* Parallel sub-tasks shown in a horizontal row when >1 */
  flex-direction: row; flex-wrap: wrap;
}
.op-phase-lanes.parallel .op-sub-task {
  flex: 1; min-width: 220px;
}

.op-sub-task {
  border: 1px solid var(--border); border-radius: var(--radius-md);
  padding: 10px 12px; cursor: pointer; transition: all 0.15s;
  background: var(--white);
}
.op-sub-task:hover { border-color: var(--blue); }
.op-sub-task.op-status-running { border-color: var(--blue); background: var(--blue-light); }
.op-sub-task.op-status-done { border-color: var(--green); background: var(--green-light); }
.op-sub-task.op-status-failed { border-color: var(--red); background: var(--red-light); }

.op-st-header { display: flex; align-items: center; gap: 10px; }
.op-st-icon { font-size: 16px; }
.op-st-info { flex: 1; }
.op-st-id { font-size: 10.5px; color: var(--text-tertiary); display: block; }
.op-st-label { font-size: 13px; font-weight: 500; }
.op-st-right { display: flex; align-items: center; gap: 8px; }
.op-st-agent { font-size: 10.5px; color: var(--text-tertiary); }
.op-expand-toggle { font-size: 10px; color: var(--text-tertiary); }

.op-st-steps { margin-top: 10px; border-top: 1px solid var(--border); padding-top: 10px; }
.op-step {
  display: flex; align-items: flex-start; gap: 8px;
  padding: 4px 0; font-size: 12px;
}
.op-step-icon { width: 16px; flex-shrink: 0; text-align: center; }
.op-step-label { flex: 1; }
.op-step-detail { font-size: 11px; color: var(--text-secondary); font-style: italic; display: block; margin-top: 2px; }
.op-step-ts { font-size: 10px; color: var(--text-tertiary); flex-shrink: 0; }

.op-step.op-step-done .op-step-icon { color: var(--green); }
.op-step.op-step-running .op-step-icon { color: var(--blue); }
.op-step.op-step-failed .op-step-icon { color: var(--red); }
.op-step.op-step-queued { opacity: 0.5; }

.op-jenkins-params { margin-top: 8px; }
.op-params-label { font-size: 11px; color: var(--text-tertiary); margin: 0 0 4px; }
```

---

## 7. Frontend: Mock Simulation Ticker (frontend/src/hooks/useSubTaskTicker.js — NEW FILE)

The frontend polls running sub-tasks, calling `/sub-tasks/{id}/tick`
and updating the UI as steps advance.

```js
import { useEffect, useRef, useCallback } from 'react'
import { api } from '../api.js'

/**
 * useSubTaskTicker(taskId, subTasks, onUpdate)
 *
 * While a task is "running", polls each running sub-task every
 * TICK_INTERVAL_MS and calls onUpdate(updatedSubTask) for each tick.
 *
 * Automatically stops polling when:
 *   - task status is not "running"
 *   - all sub-tasks are done or failed
 *   - component unmounts
 *
 * TICK_INTERVAL_MS is set to 2000ms (2 seconds) for the demo —
 * makes the UI feel live without being too fast to follow.
 * When real Jenkins webhooks land, replace this polling with
 * a WebSocket or SSE listener and delete this hook.
 */
const TICK_INTERVAL_MS = 2000

export function useSubTaskTicker(taskStatus, subTasks, onUpdate) {
  const intervalRef = useRef(null)

  const tick = useCallback(async () => {
    const running = (subTasks || []).filter(st => st.status === 'running')
    if (running.length === 0) return

    await Promise.all(running.map(async st => {
      try {
        const result = await api.tickSubTask(st.sub_task_id)
        onUpdate(result.sub_task)
      } catch (e) {
        console.warn('tick failed for', st.sub_task_id, e)
      }
    }))
  }, [subTasks, onUpdate])

  useEffect(() => {
    if (taskStatus !== 'running') {
      clearInterval(intervalRef.current)
      return
    }

    const allDone = (subTasks || []).every(st => ['done', 'failed'].includes(st.status))
    if (allDone) {
      clearInterval(intervalRef.current)
      return
    }

    intervalRef.current = setInterval(tick, TICK_INTERVAL_MS)
    return () => clearInterval(intervalRef.current)
  }, [taskStatus, subTasks, tick])
}
```

---

## 8. Frontend: TaskDetail.jsx (MAJOR MODIFY)

TaskDetail is now the main page for monitoring execution. It shows:

```
[Task header: TASK-20392 · TRB-16996 · INTEG · High]
[Status badge] [Created by] [Approver]

── Request Details (always visible) ──
Environment, Jira, description, branches, sections summary

── Approval Panel (visible when status=pending_approval) ──
[Approval chain stepper]
[Approve/Reject buttons per role]

── [After final approval: Orchestrator Plan View] ──
Phase 1 → Phase 2 → Phase 3 flow
Each sub-task expandable showing its steps + detail + logs

── [DevOps only: Jenkins Parameters per sub-task] ──
```

Key behaviors:
- **Auto-refresh:** Use `useSubTaskTicker` when task.status === "running"
  to poll and update sub-task state every 2 seconds
- **Load:** `GET /tasks/{taskId}/full` on mount and after each approval
- **Orchestrator plan:** Visible to ALL roles once task is running
  - Developer: sees status only (no step detail, no params)
  - Dev Lead / QA: sees phase layout + sub-task status (no params)
  - DevOps: full detail — steps, detail text, params, logs

Wire the ticker:
```jsx
// In TaskDetail.jsx
import { useSubTaskTicker } from '../hooks/useSubTaskTicker.js'

const [task, setTask] = useState(null)
const [subTasks, setSubTasks] = useState([])

useSubTaskTicker(task?.status, subTasks, (updatedSt) => {
  setSubTasks(prev => prev.map(st =>
    st.sub_task_id === updatedSt.sub_task_id ? updatedSt : st
  ))
})
```

---

## 9. Frontend: History.jsx (MODIFY)

History expandable rows already exist. Modify the expanded row to show:

For all roles: Environment, Jira, description, branches, approver, approval status
For Dev Lead / QA: + Approval panel (if it's their turn)
For DevOps: + Approval panel (if it's their turn) + compact orchestrator plan summary

The compact plan summary in History rows (not full OrchestratorPlan component):
```jsx
<div className="history-plan-summary">
  {plan.phases.map(phase => (
    <div key={phase.phase} className="hps-phase">
      <span className="hps-phase-label">Phase {phase.phase}</span>
      <div className="hps-sub-tasks">
        {phase.sub_task_ids.map(stId => (
          <span key={stId} className={`hps-chip hps-chip-${stMap[stId]?.status || 'queued'}`}>
            {SECTION_ICONS[stMap[stId]?.section]} {stMap[stId]?.label}
          </span>
        ))}
      </div>
    </div>
  ))}
</div>
```

---

## 10. api.js additions (MODIFY)

```js
// Sub-tasks
getSubTask: (subTaskId) => request(`/sub-tasks/${subTaskId}`),
tickSubTask: (subTaskId) => request(`/sub-tasks/${subTaskId}/tick`, { method: 'POST' }),
getOrchestratorPlan: (taskId) => request(`/tasks/${taskId}/orchestrator-plan`),
getTaskFull: (taskId) => request(`/tasks/${taskId}/full`),
```

---

## 11. Seed data update (backend/main.py demo/seed — MODIFY)

Update `seed_demo_data()` to create tasks that cover all visual states:

```python
# Task 1: pending_approval (new, waiting for dev_lead)
# Task 2: running — Phase 1 (DB) in progress
# Task 3: running — Phase 3 (Build) in progress, Phase 1+2 done
# Task 4: done (all sub-tasks complete)
# Task 5: blocked (one sub-task failed)
# Task 6: rejected (dev_lead rejected with comment)
```

After creating each task, use `db.update_sub_task_status()` and
`db.advance_sub_task_step()` to move sub-tasks to the correct state
for each scenario so the seed data already looks like a real
execution in progress.

---

## 12. Implementation order for Cursor

Implement in exactly this order — each step is independently testable:

1. `backend/db.py` — add sub-task model, counter, and new functions
   - Test: `create_task()` returns sub-tasks with correct agents, steps, and orchestrator_plan
   - Test: orchestrator_plan phases are correct (DB→YAML/Phrases→Build order)

2. `backend/mock_executor.py` — new file
   - Test: `tick_sub_task()` advances steps correctly across 10 sequential calls
   - Test: `mock_validate_section()` returns deterministic results (same input → same output)

3. `backend/main.py` — new endpoints
   - Test: `POST /tasks/validate` returns per-link results
   - Test: `POST /sub-tasks/{id}/tick` advances steps and returns tick result

4. `backend/main.py` seed data — update for all 6 visual states

5. `frontend/src/hooks/useSubTaskTicker.js` — new hook

6. `frontend/src/components/OrchestratorPlan.jsx` — new component
   - Test: renders correct phase layout with seed data

7. `frontend/src/pages/NewRequest.jsx` — validation badges + per-link error display

8. `frontend/src/App.jsx` — add RoleContext + role switcher in sidebar

9. `frontend/src/pages/TaskDetail.jsx` — wire full view + ticker + role-based detail

10. `frontend/src/pages/History.jsx` — compact plan summary in expanded rows

---

## 13. Acceptance criteria (all must pass before merging)

### Validation
- [ ] Clicking Validate shows per-link ✓/✕ badges next to each service selection
- [ ] Each failing link shows the exact error text directly below it (not in a global alert)
- [ ] Submit button stays disabled when any link has ✕
- [ ] Editing any form field after validation clears all badges and disables Submit
- [ ] Clicking Validate again after edits re-runs validation and shows fresh results
- [ ] Validation results are deterministic: same service_key + section always produces same result in demo (no random per-run variance)

### Sub-task model
- [ ] A form submission with 2 build links + 2 yaml links + 1 db link creates 5 sub-tasks, not 3 jobs
- [ ] Sub-task IDs are globally unique per task (ST-1, ST-2, ...)
- [ ] Orchestrator plan shows Phase 1 (DB), Phase 2 (YAML), Phase 3 (Build) regardless of form input order
- [ ] DB sub-task always in Phase 1, YAML in Phase 2, Build in Phase 3

### Orchestrator plan view
- [ ] Visible in TaskDetail for ALL roles once task is running
- [ ] Developer sees phase/sub-task status but NOT step detail text or Jenkins params
- [ ] DevOps sees full step detail, detail text from mock, and Jenkins params
- [ ] Clicking a sub-task box expands its step list inline (no page navigation)
- [ ] Running sub-tasks show a live-updating step list (updates every 2 seconds via ticker)
- [ ] Parallel sub-tasks within a phase are displayed side-by-side (horizontal layout)
- [ ] Phase 2 has "↓ after phase 1 completes" separator arrow between phases

### Role-based views
- [ ] Switching role selector changes which approval buttons appear
- [ ] Developer sees read-only approval status (no approve/reject buttons)
- [ ] Dev Lead sees approve/reject only when it's their turn in the chain
- [ ] QA approve/reject only appears when code freeze is ON and dev_lead has approved
- [ ] DevOps approve/reject appears as the final stage
- [ ] Code Freeze toggle only visible in sidebar/Home when role=devops

### Mock simulation
- [ ] Ticking a running build sub-task 6 times completes all 6 steps in sequence
- [ ] Each step shows realistic mock detail text (see Section 2 step templates)
- [ ] ~8% of sub-tasks show a realistic failure on a middle step (not first or last)
- [ ] After a Phase 1 sub-task completes, Phase 2 sub-tasks auto-start
- [ ] After a Phase 2 sub-task completes (and all Phase 2 done), Phase 3 auto-starts
- [ ] A failed sub-task blocks its phase — Phase 2 does not start if Phase 1 failed