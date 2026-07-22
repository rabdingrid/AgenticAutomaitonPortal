# 04 — DB Orchestrator (code walkthrough)

Jenkins Liquibase run after DB validation. Paths under `deployment-portal/backend/`.

---

## 1. Files involved

| File | Role |
|------|------|
| `main.py` | `_maybe_start_orchestrator` |
| `db.py` | `_sync_db_jenkins_params`, `record_agent_event` |
| `orchestrator/runner.py` | Phase 1, `_run_sub_task`, capacity gate |
| `orchestrator/graph.py` | `agent_type=db` branch in `build_params` |
| `orchestrator/jenkins_params.py` | `build_db_liquibase_params`, `map_db_microservice` |
| `orchestrator/jenkins_client.py` | Trigger / poll / console |
| `config/orchestrator_agents.json` | `db` agent |
| `config/jenkins_jobs.json` | `db_script_automation_liquibase` |
| `config/jenkins_db_service_map.json` | Label → `MICROSERVICE` |

Unlike YAML/Phrases: **one Jenkins job per DB service** (not per changeset). Approved changesets are selected inside the Liquibase job from the SQL files.

---

## 2. Sync params onto sub-task

```python
# db.py — _sync_db_jenkins_params (called from _sync_jenkins_subtask_params)
# For each db sub-task:
sub_task["jenkins_params"] = jenkins_params.build_db_liquibase_params(
    microservice=map_db_microservice(label),
    environment=...,
    action="Update",   # or Rollback if configured
    branch=release_branch,
    release_tag=task["release_tag"],
)
# also sets job path for DB-Script-Automation-Liquibase
```

**Explain:** Runs when validation completes and/or DevOps sets `RELEASE_TAG`. Graph later reads these params in `build_params`.

---

## 3. Phase 1 execution

```python
# runner.run_task
for phase in phases:  # phase 1 = Database migrations, section db
    _run_phase_tasks(task_id, phase_tasks, demo_scenario)

# _run_phase_tasks — db is not yaml/phrases, so:
if len(other_tasks) > 1:
    _run_sub_tasks_parallel(...)  # one thread each
else:
    _run_sub_task(...)
```

**Explain:** Multiple DB services can start threads together, but `_CapacityGate` for `db` defaults to **max_concurrent: 1**, so extras sit `queued`.

```python
# runner._agent_type_for
if section == "build":
    return sub_type  # microservice|portal
return section       # "db"
```

---

## 4. Graph: `build_params` for db

```python
# graph.py — build_params
elif agent_type == "db":
    params = jenkins_params.build_params_for_agent("db", ...)
```

Then same shared nodes as everyone else:

`load_context` → `build_params` → `trigger_jenkins` → `poll_jenkins` → `health_check` → `finalize_success`

On failure:

```python
# graph.analyze_failure
rule = failure_rules.classify(console_log, jenkins_result=...)
ai = ai_client.analyze_jenkins_log(service_label=..., console_tail=..., ...)
# merge into ai_report → UI
```

**Explain:** Same failure AI as Build/YAML/Phrases. DB agent often has `max_retries: 0` in config → usually no auto-retry.

---

## 5. Typical Jenkins params

| Key | From |
|-----|------|
| `MICROSERVICE` | `map_db_microservice(label)` |
| `ENVIRONMENT` | Task environment |
| `ACTION` | Update / Rollback |
| `Branch` | Release branch |
| `RELEASE_TAG` | DevOps tag |

---

## 6. Mental model

```
Phase 1
  → _sync_db_jenkins_params
  → _run_sub_task(agent_type=db) + gate
  → graph → DB-Script-Automation-Liquibase
```

Upstream validation: [03-db-validation.md](./03-db-validation.md)
