# 06 — Phrases Orchestrator (code walkthrough)

`Json_Automation_V2` via shared LangGraph. Paths under `deployment-portal/backend/`.

---

## 1. Files involved

| File | Role |
|------|------|
| `main.py` | Orchestrator kickoff |
| `db.py` | `_sync_phrases_jenkins_params`, `prepare_json_deployment_rerun` |
| `orchestrator/runner.py` | `_run_phrases_sub_task`, gate |
| `orchestrator/graph.py` | `agent_type=phrases` in `build_params` / `finalize_success` |
| `orchestrator/jenkins_params.py` | `build_json_automation_params`, maps, sort helpers |
| `config/orchestrator_agents.json` | `phrases` |
| `config/jenkins_jobs.json` | `json_automation_v2` |
| `config/jenkins_phrases_portal_map.json` | Label → `Portals` |

---

## 2. Sync: `json_deployments[]`

```python
# db._sync_phrases_jenkins_params (concept)
# From validation json_files or rediscovery on release branch:
json_deployments = [
  { "filename": "…-remove.json", "action": "Delete", "update": "Json",
    "jenkins_params": build_json_automation_params(..., action="Delete") },
  { "filename": "….json", "action": "Add", "update": "Json",
    "jenkins_params": build_json_automation_params(..., action="Add") },
]
# SchemaForms / NewSchemaForms → usually one entry, Update type only (no Action)
```

**Explain:** Stored on the phrases sub-task. Runner loops this list **serially**.

```python
# jenkins_params
def phrases_update_for_sub_type(sub_type):
    # phrases → Json, schemaforms → SchemaForms, newschemaforms → NewSchemaForms
```

---

## 3. Runner loop

```python
# runner._run_phrases_sub_task
deployments = sub_task.get("json_deployments") or [None]
for idx, dep in enumerate(deployments):
    if idx > 0:
        db.prepare_json_deployment_rerun(sub_task_id, dep)
    _run_sub_task(
        task_id, sub_task, demo_scenario,
        json_deployment=dep, json_deploy_index=idx,
    )
    if failed: break
```

**Explain:** Same pattern as YAML multi-file: reset steps + swap params for index > 0. Capacity gate default **1** concurrent phrases job.

Phase 2 runs **after all YAML** sub-tasks in `_run_phase_tasks`.

---

## 4. Graph params for phrases

```python
# graph.build_params
elif agent_type == "phrases":
    json_deployments = sub_task.get("json_deployments") or []
    if json_deployments and jidx < len(json_deployments):
        params = json_deployments[jidx].get("jenkins_params") or rebuild...
```

**Explain:** Prefer precomputed `jenkins_params` on the deployment entry (includes `Action` when Update=Json).

`finalize_success`: if more `json_deployments` remain, keep status `running`.

Failure path: same `analyze_failure` + Ollama as other agents.

---

## 5. Typical Jenkins keys

| Param | Notes |
|-------|--------|
| `Portals` | Mapped portal name |
| `Update` | Json / SchemaForms / NewSchemaForms |
| `Action` | Add/Delete only for Json |
| `PhrasesBranch`, `Environment`, `Release_Version` | From task / tag |

---

## 6. Mental model

```
Phase 2 (after YAML)
  → _run_phrases_sub_task
    → for each json_deployment: gate → graph(phrases) → Json_Automation_V2
```

Upstream: [05-phrases-validation.md](./05-phrases-validation.md)
