# 09 — Master Orchestrator (code walkthrough)

End-to-end coordination: plan → approve → phases → LangGraph → Jenkins → UI. Paths under `deployment-portal/backend/`.

---

## 1. Files involved (map)

| File | Responsibility |
|------|----------------|
| `main.py` | `_maybe_start_orchestrator`, approve, retry API |
| `db.py` | `_build_orchestrator_plan`, `_sync_jenkins_subtask_params`, `_sync_yaml/db/phrases_*`, `record_agent_event`, `advance_orchestrator_phase` |
| `orchestrator/runner.py` | `run_task` / `run_task_async`, phases, capacity gate |
| `orchestrator/graph.py` | Shared `orchestrator_graph` nodes + routing |
| `orchestrator/jenkins_params.py` | All Jenkins param builders |
| `orchestrator/jenkins_client.py` | HTTP to Jenkins |
| `orchestrator/failure_rules.py` | Classify console failures |
| `orchestrator/config.py` | Load `orchestrator_agents.json` |
| `ai_client.py` | Jenkins failure AI (+ DB validation elsewhere) |
| `config/orchestrator_agents.json` | enabled, max_concurrent, retries |
| `config/jenkins_jobs.json` | Job paths |
| `validators/validation.py` | Upstream report that feeds sync |

---

## 2. Plan creation at task create

```python
# db.py
_PHASE_DEFS = [
    (1, "Database migrations", ["db"]),
    (2, "YAML & Json & SchemaForms", ["yaml", "phrases"]),
    (3, "Build & deploy", ["build"]),
]

# _build_orchestrator_plan(task) groups sub_task_ids into phases[]
```

**Explain:** Written once onto `task.orchestrator_plan`. Runner always executes phase 1 then 2 then 3.

---

## 3. Approval → start runner

```python
# main.py — approve_task
task = db.apply_approval_decision(..., release_tag=payload.release_tag)
_maybe_start_orchestrator(task)

def _maybe_start_orchestrator(task):
    if task["status"] == "running" and not task.get("current_stage"):
        if orchestrator_runner.is_langgraph_mode():
            orchestrator_runner.run_task_async(task["task_id"])
```

**Explain:** DevOps must supply `release_tag` when required. When the last stage clears, status becomes `running` with no `current_stage` → background `run_task`.

Sync params (yaml_deployments, json_deployments, db/build jenkins_params) happen via `_sync_jenkins_subtask_params` when the report is saved and/or tag is set.

---

## 4. `run_task` — phase loop

```python
# orchestrator/runner.py
def run_task(task_id, demo_scenario=""):
    task = db.get_task_with_sub_tasks(task_id)
    for phase in task["orchestrator_plan"]["phases"]:
        phase_tasks = [st_map[sid] for sid in phase["sub_task_ids"] if sid in st_map]
        _run_phase_tasks(task_id, phase_tasks, demo_scenario)
    db.advance_orchestrator_phase(task_id)
```

### Inside `_run_phase_tasks`

```python
yaml_tasks = [t for t in phase_tasks if t["section"] == "yaml"]
phrases_tasks = [t for t in phase_tasks if t["section"] == "phrases"]
other_tasks = [t for t in phase_tasks if t["section"] not in ("yaml", "phrases")]

for sub_task in yaml_tasks:
    _run_yaml_sub_task(...)      # serial multi-file inside
for sub_task in phrases_tasks:
    _run_phrases_sub_task(...)   # serial multi-deploy inside
if len(other_tasks) > 1:
    _run_sub_tasks_parallel(...) # db / build threads
else:
    _run_sub_task(...)
```

**Explain:**

| Phase | Behavior |
|-------|----------|
| 1 DB | Parallel threads, gated to 1 db job |
| 2 | **All YAML first**, then **all Phrases**; each may loop files |
| 3 Build | Parallel threads, gated 2 MS + 1 portal |

---

## 5. Capacity gate (shared by all agents)

```python
# runner._CapacityGate
# start only if: mine + external_jenkins < limit
# limit from orchestrator_agents.json or ORCHESTRATOR_MAX_* env
```

Defaults: microservice **2**, everyone else **1**. Queued sub-tasks poll every `ORCHESTRATOR_SLOT_POLL_INTERVAL` (10s).

---

## 6. Shared LangGraph (`graph.py`)

### Build graph wiring

```python
# conceptual
START → load_context → build_params → trigger_jenkins → poll_jenkins
poll → (running → poll) | (success → health_check → finalize) | (fail → fetch_console → analyze_failure)
analyze → retry_build → trigger  OR  build_error_report → END
```

### Persistence from every node

```python
# graph helpers call:
db.record_agent_event(sub_task_id, step_index=..., step_status=..., console_log=..., ai_report=...)
```

**Explain:** UI `OrchestratorPlan` reads these fields via `GET /tasks/{id}/full`.

### `build_params` switch

```python
if agent_type == "yaml":    # ACTION from yaml_deployment
elif agent_type == "db":    # Liquibase params
elif agent_type == "phrases":  # json_deployment.jenkins_params
else:  # microservice / portal Titan params
```

### `analyze_failure`

```python
rule = failure_rules.classify(console_log, jenkins_result=...)
ai = ai_client.analyze_jenkins_log(...)
# produce ai_report { category, summary, root_cause, remediation_steps, retryable }
```

Works for **yaml, db, phrases, microservice, portal** — same node.

### `health_check`

POC stub — always reports success after Jenkins SUCCESS. Not real ECS polling yet.

---

## 7. Jenkins client API surface

| Function | Used for |
|----------|----------|
| `trigger_build_with_parameters` | Start job |
| `get_build_info` | Poll running/success/failure |
| `get_console_text` | Live log + AI input |
| `count_active_builds` | Capacity gate |

Requires `JENKINS_ENABLED=true` + URL/user/token; else graph can mock.

---

## 8. AI usage (whole portal)

| Place | Code | AI? |
|-------|------|-----|
| DB validation | `ai_client.review_db_sql_syntax` | Yes |
| Report summary | `ai_client.summarize_report` | Optional |
| Jenkins fail | `ai_client.analyze_jenkins_log` | Yes |
| YAML/Phrases/Build validation | — | No |
| health_check | — | No |

---

## 9. Section → agent → job

| Section | agent_type | Job key / path |
|---------|------------|----------------|
| db | `db` | `DB-Script-Automation-Liquibase` |
| yaml | `yaml` | `YML_Automation_V3` |
| phrases | `phrases` | `Json_Automation_V2` |
| build / microservice | `microservice` | `Titan-Microservices` |
| build / portal | `portal` | `Titan-Portals` |

---

## 10. Mental model (full request)

```
POST /tasks
  → validation.run_validation → validation_report
  → approvals (+ RELEASE_TAG)
  → _sync_jenkins_subtask_params
  → run_task_async
      Phase1 DB  → graph(db) → Liquibase Jenkins
      Phase2 YAML → graph(yaml) × files → YML Jenkins
      Phase2 Phrases → graph(phrases) × deploys → Json Jenkins
      Phase3 Build → graph(ms|portal) queued by slots → Titan Jenkins
  → record_agent_event → UI poll
```

---

## 11. Deep dives by section

| Doc | Focus |
|-----|--------|
| [01](./01-yaml-validation.md) / [02](./02-yaml-orchestrator.md) | YAML |
| [03](./03-db-validation.md) / [04](./04-db-orchestrator.md) | DB + AI SQL |
| [05](./05-phrases-validation.md) / [06](./06-phrases-orchestrator.md) | Phrases |
| [07](./07-build-validation.md) / [08](./08-build-orchestrator.md) | Build + slots |
