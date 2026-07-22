# 02 — YAML Orchestrator (code walkthrough)

How validated YAML becomes Jenkins runs on `YML_Automation_V3`. Paths under `deployment-portal/backend/`.

---

## 1. Files involved

| File | Role |
|------|------|
| `main.py` | `_maybe_start_orchestrator` after approval |
| `db.py` | `_sync_yaml_deployments`, `prepare_yaml_deployment_rerun`, `record_agent_event` |
| `orchestrator/runner.py` | `_run_yaml_sub_task`, `_run_sub_task`, `_CapacityGate` |
| `orchestrator/graph.py` | Shared LangGraph (`agent_type=yaml`) |
| `orchestrator/jenkins_params.py` | `build_yml_automation_params`, `map_yaml_service` |
| `orchestrator/jenkins_client.py` | trigger / poll / console |
| `config/orchestrator_agents.json` | `yaml` agent: enabled, max_concurrent |
| `config/jenkins_jobs.json` | Job path for `yml_automation_v3` |
| `config/jenkins_yaml_service_map.json` | UI label → Jenkins `SERVICE` |

---

## 2. Kickoff after approvals

```python
# main.py
def _maybe_start_orchestrator(task):
    if task.get("status") == "running" and not task.get("current_stage"):
        if orchestrator_runner.is_langgraph_mode():
            orchestrator_runner.run_task_async(task["task_id"])

# called from approve_task after successful DevOps (and prior) approvals
_maybe_start_orchestrator(task)
```

**Explain:** When no approval stage remains and status is `running`, a **background thread** runs `run_task` → phases → YAML in phase 2.

`release_tag` must already be set (DevOps); `load_context` in the graph fails without it.

---

## 3. Param sync: `_sync_yaml_deployments`

**When:** `_sync_jenkins_subtask_params` (after validation report and/or DevOps tag).

**Reads:** `validation_report` → YAML items → `yaml_files[]`.

**Writes on each yaml sub-task:**

```python
# Conceptual shape written by db._sync_yaml_deployments
sub_task["yaml_deployments"] = [
    {
        "path": ".../parameters-remove.yml",
        "filename": "parameters-remove.yml",
        "action": "Remove",
        "jenkins_params": build_yml_automation_params(..., action="Remove"),
    },
    {
        "filename": "parameters.yml",
        "action": "Update",
        "jenkins_params": build_yml_automation_params(..., action="Update"),
    },
]
```

**Explain:** One Jenkins execution **per file**. Remove file gets `ACTION=Remove`. Params include mapped `SERVICE`, `ENVIRONMENT`, `Branch`, `RELEASE_TAG`.

```python
# jenkins_params.py
def yaml_action_for_filename(filename: str) -> str:
    # remove in name → "Remove", else "Update"
```

---

## 4. Runner: serial multi-file loop

```python
# orchestrator/runner.py — _run_yaml_sub_task (concept)
def _run_yaml_sub_task(task_id, sub_task, demo_scenario):
    deployments = sub_task.get("yaml_deployments") or [None]
    for idx, dep in enumerate(deployments):
        if idx > 0:
            db.prepare_yaml_deployment_rerun(sub_task_id, dep)  # reset steps, swap params
        _run_sub_task(
            task_id, sub_task, demo_scenario,
            yaml_deployment=dep, yaml_deploy_index=idx,
        )
        if sub_task failed:
            break
```

**Explain:**

- File 0 uses params already on the sub-task.  
- File 1+ calls `prepare_yaml_deployment_rerun` so UI steps reset and `jenkins_params` become the next file’s.  
- Stops if one Jenkins run fails (no silent skip of remaining files).

Phase ordering (yaml before phrases):

```python
# runner._run_phase_tasks
for sub_task in yaml_tasks:
    _run_yaml_sub_task(...)
for sub_task in phrases_tasks:
    _run_phrases_sub_task(...)
# then parallel "other" (not yaml/phrases)
```

---

## 5. Slot gate then graph

```python
# runner._run_sub_task
agent_type = _agent_type_for(sub_task)  # section yaml → "yaml"
limit = _slot_limit(agent_type)        # default 1 from orchestrator_agents.json

while not _GATE.try_reserve(agent_type, job_path, limit):
    _mark_queued(sub_task, ...)        # UI: status=queued
    time.sleep(ORCHESTRATOR_SLOT_POLL_INTERVAL)  # default 10s

try:
    _dispatch_sub_task(sub_task)       # status=running
    _invoke_graph(..., yaml_deployment=..., yaml_deploy_index=...)
finally:
    _GATE.release(agent_type)
```

**Explain:** Even if phase fans out, YAML only allows **1** concurrent Jenkins job of that type (configurable). Extras wait; they are not dropped.

`_invoke_graph` builds `AgentState` and calls `orchestrator_graph.invoke(state)`.

---

## 6. Graph branching for YAML

```python
# orchestrator/graph.py — build_params node (yaml branch)
if agent_type == "yaml":
    yaml_action = deployments[idx].get("action", "Update")
    params = jenkins_params.build_params_for_agent(
        "yaml", ..., yaml_action=yaml_action, ...
    )
```

**Explain:** `ACTION` on the Jenkins job comes from the current deployment’s `action` field.

Happy path nodes: `load_context` → `build_params` → `trigger_jenkins` → `poll_jenkins` → `health_check` → `finalize_success`.

```python
# finalize_success — yaml/phrases
if more deployments remain:
    keep status "running"   # runner will start next file
else:
    mark done
```

Failure: `fetch_console_log` → `analyze_failure` → `ai_client.analyze_jenkins_log` (same as other agents).

---

## 7. Jenkins client calls

| Function | When |
|----------|------|
| `trigger_build_with_parameters(job, params)` | `trigger_jenkins` |
| `get_build_info` / status | `poll_jenkins` loop |
| `get_console_text` | Live tail + failure analysis |
| `count_active_builds` | Capacity gate |

---

## 8. Mental model

```
Approve + RELEASE_TAG
  → _sync_yaml_deployments (yaml_deployments[])
  → run_task → phase 2
    → _run_yaml_sub_task
      → for each file: _run_sub_task → gate → graph(yaml)
        → YML_Automation_V3
```

Upstream: [01-yaml-validation.md](./01-yaml-validation.md) · Master: [09-master-orchestrator.md](./09-master-orchestrator.md)
