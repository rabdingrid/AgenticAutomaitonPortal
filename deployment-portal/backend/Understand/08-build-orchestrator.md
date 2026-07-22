# 08 — Build Orchestrator (code walkthrough)

Titan Microservices / Portals Jenkins via shared graph + capacity queue. Paths under `deployment-portal/backend/`.

---

## 1. Files involved

| File | Role |
|------|------|
| `main.py` | Kickoff + `retry_sub_task` |
| `db.py` | Build params on sub-task, `reset_sub_task_for_retry`, `record_agent_event` |
| `orchestrator/runner.py` | Parallel phase 3, `_CapacityGate`, `_run_sub_task` |
| `orchestrator/graph.py` | Full agent lifecycle + `analyze_failure` |
| `orchestrator/jenkins_params.py` | `build_titan_microservices_params`, `build_titan_portals_params` |
| `orchestrator/jenkins_client.py` | Trigger, poll, console, `count_active_builds` |
| `orchestrator/failure_rules.py` | Deterministic failure categories |
| `ai_client.py` | `analyze_jenkins_log` on failure |
| `config/orchestrator_agents.json` | microservice `max_concurrent: 2`, portal `1` |

---

## 2. Agent type from sub-task

```python
# runner._agent_type_for
def _agent_type_for(sub_task):
    if sub_task.get("section") == "build":
        return sub_task.get("sub_type")  # "microservice" | "portal"
    return sub_task.get("section")
```

**Explain:** Build is the only section where agent_type ≠ section name.

---

## 3. Capacity gate (why builds queue)

```python
# runner._CapacityGate.try_reserve
mine = self._inflight.get(agent_type, 0)
external = max(0, jenkins_client.count_active_builds(job_path) - mine)
if mine + external < limit:
    self._inflight[agent_type] = mine + 1
    return True
return False
```

**Explain:**

- Counts **this process** + **everyone else’s** Jenkins builds for that job.  
- Microservice limit 2, portal 1 (independent pools).  
- Failed reserve → `_mark_queued` + sleep 10s → retry. Work waits; it is not discarded.

```python
# _run_sub_task
while not _GATE.try_reserve(...):
    _mark_queued(...)
    time.sleep(_SLOT_POLL_INTERVAL)
try:
    _dispatch_sub_task(...)
    _invoke_graph(...)
finally:
    _GATE.release(agent_type)
```

---

## 4. Phase 3 parallel fan-out

```python
# runner._run_phase_tasks — build falls into other_tasks
_run_sub_tasks_parallel(task_id, other_tasks, demo_scenario)
# each sub-task = one thread; gate serializes actual Jenkins starts
```

**Explain:** Tax + Shipping can both be “in the phase”, but only 2 microservice jobs hit Jenkins at once.

---

## 5. Graph lifecycle (build)

```python
# graph nodes (shared)
load_context      # requires RELEASE_TAG
build_params      # titan microservices OR portals builder
trigger_jenkins
poll_jenkins      # loop while status == running
health_check      # POC stub: always "HTTP 200"
finalize_success
```

```python
# build_params for build agents
params = jenkins_params.build_params_for_agent(agent_type, ...)
# MergeID from MR / blank if build_only
# Service or Portal, Environment, Branch, RELEASE_TAG, ...
```

### Failure analysis (same pattern for YAML/DB/Phrases)

```python
# graph.analyze_failure
rule = failure_rules.classify(console_log, jenkins_result=jenkins_result)
ai = ai_client.analyze_jenkins_log(
    service_label=...,
    jenkins_params=...,
    console_tail=console_log,
    build_number=...,
    rule_hint=rule if high_confidence else None,
)
# ai_report → summary, root_cause, remediation_steps → UI
```

**Explain:** Rules may lock category at ≥90% confidence; Ollama still helps with wording. Retries only for categories listed on the agent (e.g. `heap_oom`, `transient_infra`).

---

## 6. Manual retry

```python
# runner.retry_sub_task_async → reset_sub_task_for_retry → _run_sub_task
# goes through the same CapacityGate
```

---

## 7. Mental model

```
Phase 3
  → threads per build sub-task
  → queue until microservice≤2 / portal≤1 free in Jenkins
  → graph → Titan-Microservices | Titan-Portals
  → on fail: console + rules + Ollama report
```

Upstream: [07-build-validation.md](./07-build-validation.md) · Master: [09](./09-master-orchestrator.md)
