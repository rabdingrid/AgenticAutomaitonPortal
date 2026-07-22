"""
orchestrator_runner.py — Bridge between the portal and the LangGraph orchestrator.

When ORCHESTRATOR_MODE=langgraph, the portal stops relying on frontend `/tick`
polling and instead runs the compiled agent graph in a background thread after
DevOps approval. Each graph node persists progress to db.json (via
db.record_agent_event), so the existing `/tasks/{id}/full` poll surfaces live state.

Modes (ORCHESTRATOR_MODE):
    mock       — legacy: frontend ticks mock_executor (default)
    langgraph  — run the LangGraph agent graph in the background (this module)

Concurrency queue (Jenkins-aware):
    Builds are gated by a PROCESS-WIDE slot limit per agent type so we never
    hammer Jenkins. Defaults: 2 microservice + 1 portal at a time (configurable
    via config/orchestrator_agents.json `max_concurrent`, or the env overrides
    ORCHESTRATOR_MAX_MICROSERVICE / ORCHESTRATOR_MAX_PORTAL). Microservice and
    portal slots are independent, so up to 2 microservice AND 1 portal can run
    together.

    Crucially the gate is Jenkins-aware: before a build starts it polls Jenkins
    for how many builds of that job are already running/queued (by ANYONE — this
    portal, another instance, or a human in the Jenkins UI) and counts those
    against the limit. So if a portal build is already running in Jenkins, a new
    portal build waits until that slot frees. Extra builds sit in a queue (shown
    as `queued` in the UI) and re-check every ORCHESTRATOR_SLOT_POLL_INTERVAL
    seconds until a real slot opens up.

Only agents marked enabled in config/orchestrator_agents.json run through the
graph; disabled sections are auto-completed so phases still advance.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import db
from orchestrator import config as orchestrator_config
from orchestrator import jenkins_client, jenkins_params

_RECURSION_LIMIT = int(os.getenv("ORCHESTRATOR_RECURSION_LIMIT", "100"))

# How often a queued build re-checks Jenkins for a free slot (seconds).
_SLOT_POLL_INTERVAL = float(os.getenv("ORCHESTRATOR_SLOT_POLL_INTERVAL", "10"))

_ENV_LIMIT_OVERRIDES = {
    "microservice": "ORCHESTRATOR_MAX_MICROSERVICE",
    "portal": "ORCHESTRATOR_MAX_PORTAL",
    "yaml": "ORCHESTRATOR_MAX_YAML",
    "db": "ORCHESTRATOR_MAX_DB",
    "phrases": "ORCHESTRATOR_MAX_PHRASES",
}


def _slot_limit(agent_type: str) -> int:
    """Resolve the concurrency cap for an agent type (env override wins). 0 = unlimited."""
    env_key = _ENV_LIMIT_OVERRIDES.get(agent_type)
    if env_key:
        raw = os.getenv(env_key, "").strip()
        if raw:
            try:
                return max(0, int(raw))
            except ValueError:
                pass
    return orchestrator_config.max_concurrent(agent_type)


def _job_path_for(agent_type: str) -> str:
    """Jenkins job path used for the live capacity check (empty if not a build agent)."""
    try:
        return jenkins_params.job_path_for_agent(agent_type)
    except Exception:
        return ""


class _CapacityGate:
    """Process-wide, Jenkins-aware build slots per agent type.

    A build of a given type may start only when

        (our in-flight builds of this type)
      + (builds of the same Jenkins job already running/queued by anyone else)
      <  limit

    ``count_active_builds`` reports every live build of the job in Jenkins, so
    builds triggered outside this portal also consume slots. We can't perfectly
    label which live builds are "ours", so we subtract our own in-flight tally
    from Jenkins' active count to estimate the external ones (never below zero).
    Reservation is done under a lock so two threads can't claim the last slot.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: dict[str, int] = {}

    def inflight(self, agent_type: str) -> int:
        with self._lock:
            return self._inflight.get(agent_type, 0)

    def try_reserve(self, agent_type: str, job_path: str, limit: int) -> bool:
        with self._lock:
            mine = self._inflight.get(agent_type, 0)
            external = self._external_running(job_path, mine)
            if mine + external < limit:
                self._inflight[agent_type] = mine + 1
                return True
            return False

    def release(self, agent_type: str) -> None:
        with self._lock:
            self._inflight[agent_type] = max(0, self._inflight.get(agent_type, 0) - 1)

    @staticmethod
    def _external_running(job_path: str, mine: int) -> int:
        if not job_path:
            return 0
        active = jenkins_client.count_active_builds(job_path)
        return max(0, active - mine)


_GATE = _CapacityGate()


def orchestrator_mode() -> str:
    return os.getenv("ORCHESTRATOR_MODE", "mock").lower().strip()


def is_langgraph_mode() -> bool:
    return orchestrator_mode() == "langgraph"


def _agent_type_for(sub_task: dict[str, Any]) -> str:
    if sub_task.get("section") == "build":
        return sub_task.get("sub_type") or "microservice"
    return sub_task.get("section", "")


def _auto_complete(sub_task: dict[str, Any], reason: str) -> None:
    """Mark a sub-task done without running an agent (disabled section in POC)."""
    sub_task_id = sub_task["sub_task_id"]
    steps = sub_task.get("steps", [])
    for i in range(len(steps)):
        db.record_agent_event(sub_task_id, step_index=i, step_status="done",
                              step_detail="Skipped (agent not enabled)")
    db.record_agent_event(sub_task_id, status="done", log_lines=[reason])


def _dispatch_sub_task(sub_task: dict[str, Any]) -> None:
    """Mark a sub-task running when it acquires a build slot."""
    sub_task_id = sub_task["sub_task_id"]
    db.record_agent_event(
        sub_task_id,
        status="running",
        step_index=0,
        step_status="running",
        log_lines=[f"Build slot acquired — dispatched to {sub_task.get('agent', 'agent')}"],
    )


def _mark_queued(sub_task: dict[str, Any], agent_type: str, limit: int) -> None:
    """Show the sub-task waiting in the queue for a free build slot."""
    db.record_agent_event(
        sub_task["sub_task_id"],
        status="queued",
        log_lines=[
            f"Queued — waiting for a free {agent_type} build slot "
            f"(max {limit} running in Jenkins at once, includes builds already running)…"
        ],
    )


def _invoke_graph(
    task_id: str,
    sub_task: dict[str, Any],
    agent_type: str,
    demo_scenario: str,
    retry_count: int = 0,
    *,
    yaml_deployment: dict[str, Any] | None = None,
    yaml_deploy_index: int = 0,
    json_deployment: dict[str, Any] | None = None,
    json_deploy_index: int = 0,
) -> None:
    sub_task_id = sub_task["sub_task_id"]
    task = db.get_task(task_id) or {}
    state: dict[str, Any] = {
        "task_id": task_id,
        "sub_task_id": sub_task_id,
        "agent_type": agent_type,
        "service_label": sub_task.get("label") or sub_task.get("service_key") or "service",
        "release_tag": (task.get("release_tag") or "").strip(),
        "retry_count": retry_count,
        "yaml_deploy_index": yaml_deploy_index,
        "json_deploy_index": json_deploy_index,
    }
    if yaml_deployment:
        state["yaml_deployment"] = yaml_deployment
    if json_deployment:
        state["json_deployment"] = json_deployment
    if demo_scenario:
        state["demo_scenario"] = demo_scenario

    # Imported lazily so the portal still boots if langgraph isn't installed.
    from orchestrator.graph import orchestrator_graph

    try:
        orchestrator_graph.invoke(state, config={"recursion_limit": _RECURSION_LIMIT})
    except Exception as exc:  # noqa: BLE001 — surface any agent crash to the UI
        db.record_agent_event(sub_task_id, status="failed",
                              log_lines=[f"Orchestrator agent crashed: {exc}"])


def _run_yaml_sub_task(
    task_id: str,
    sub_task: dict[str, Any],
    demo_scenario: str,
    retry_count: int = 0,
) -> None:
    """Run all YAML files for one service serially (Update then Remove, etc.)."""
    deployments = sub_task.get("yaml_deployments") or [
        {"action": "Update", "filename": "parameters.yml", "jenkins_params": sub_task.get("jenkins_params")},
    ]
    for idx, dep in enumerate(deployments):
        fresh = db.get_sub_task(sub_task["sub_task_id"]) or sub_task
        if fresh.get("status") == "failed":
            return
        if idx > 0:
            db.prepare_yaml_deployment_rerun(sub_task["sub_task_id"], dep, idx)
        _run_sub_task(
            task_id, fresh, demo_scenario, retry_count,
            yaml_deployment=dep, yaml_deploy_index=idx,
        )
        after = db.get_sub_task(sub_task["sub_task_id"])
        if after and after.get("status") == "failed":
            return


def _run_phrases_sub_task(
    task_id: str,
    sub_task: dict[str, Any],
    demo_scenario: str,
    retry_count: int = 0,
) -> None:
    """Run Json / SchemaForms / NewSchemaForms deploys serially (Delete before Add for Json)."""
    deployments = sub_task.get("json_deployments") or [
        {"update": "Json", "action": "Add", "jenkins_params": sub_task.get("jenkins_params")},
    ]
    for idx, dep in enumerate(deployments):
        fresh = db.get_sub_task(sub_task["sub_task_id"]) or sub_task
        if fresh.get("status") == "failed":
            return
        if idx > 0:
            db.prepare_json_deployment_rerun(sub_task["sub_task_id"], dep, idx)
        _run_sub_task(
            task_id, fresh, demo_scenario, retry_count,
            json_deployment=dep, json_deploy_index=idx,
        )
        after = db.get_sub_task(sub_task["sub_task_id"])
        if not after or after.get("status") == "failed":
            return
        if after.get("status") == "done" and idx < len(deployments) - 1:
            continue


def _run_sub_task(
    task_id: str, sub_task: dict[str, Any], demo_scenario: str, retry_count: int = 0,
    *,
    yaml_deployment: dict[str, Any] | None = None,
    yaml_deploy_index: int = 0,
    json_deployment: dict[str, Any] | None = None,
    json_deploy_index: int = 0,
) -> None:
    agent_type = _agent_type_for(sub_task)

    if not orchestrator_config.is_agent_enabled(agent_type):
        _auto_complete(sub_task, f"{agent_type or 'section'} agent not enabled — auto-completed (POC)")
        return

    # Gate on a process-wide, Jenkins-aware build slot so we never exceed the
    # per-type limit (e.g. 2 microservice + 1 portal) — counting builds already
    # running in Jenkins by anyone. Extra builds sit in the queue and re-poll
    # until a slot actually frees (a running build of the same type finishes).
    limit = _slot_limit(agent_type)
    invoke_kw = {
        "yaml_deployment": yaml_deployment,
        "yaml_deploy_index": yaml_deploy_index,
        "json_deployment": json_deployment,
        "json_deploy_index": json_deploy_index,
    }
    if limit <= 0:
        _dispatch_sub_task(sub_task)
        _invoke_graph(task_id, sub_task, agent_type, demo_scenario, retry_count, **invoke_kw)
        return

    job_path = _job_path_for(agent_type)
    queued_logged = False
    while not _GATE.try_reserve(agent_type, job_path, limit):
        if not queued_logged:
            _mark_queued(sub_task, agent_type, limit)
            queued_logged = True
        time.sleep(_SLOT_POLL_INTERVAL)
    try:
        _dispatch_sub_task(sub_task)
        _invoke_graph(task_id, sub_task, agent_type, demo_scenario, retry_count, **invoke_kw)
    finally:
        _GATE.release(agent_type)


def _run_phase_tasks(
    task_id: str,
    phase_tasks: list[dict[str, Any]],
    demo_scenario: str,
) -> None:
    """Run phase sub-tasks: YAML and Json & SchemaForms serial; others parallel when configured."""
    yaml_tasks = sorted(
        [t for t in phase_tasks if t.get("section") == "yaml"],
        key=lambda s: s.get("order", 0),
    )
    phrases_tasks = sorted(
        [t for t in phase_tasks if t.get("section") == "phrases"],
        key=lambda s: s.get("order", 0),
    )
    other_tasks = sorted(
        [t for t in phase_tasks if t.get("section") not in ("yaml", "phrases")],
        key=lambda s: s.get("order", 0),
    )

    for sub_task in yaml_tasks:
        _run_yaml_sub_task(task_id, sub_task, demo_scenario)

    for sub_task in phrases_tasks:
        _run_phrases_sub_task(task_id, sub_task, demo_scenario)

    if not other_tasks:
        return
    if len(other_tasks) > 1:
        _run_sub_tasks_parallel(task_id, other_tasks, demo_scenario)
    else:
        _run_sub_task(task_id, other_tasks[0], demo_scenario)


def run_task(task_id: str, demo_scenario: str = "") -> None:
    """Run sub-tasks phase-by-phase; items within a parallel phase run concurrently.

    Phases are sequential (DB → config/phrases → build). Sub-tasks inside a phase
  marked ``parallel`` in the orchestrator plan each get their own thread so
    e.g. Tax + Shipping builds trigger Jenkins at the same time.
    """
    task = db.get_task_with_sub_tasks(task_id)
    if not task:
        return
    demo_scenario = demo_scenario or os.getenv("ORCHESTRATOR_DEMO_SCENARIO", "").strip()

    plan = task.get("orchestrator_plan") or {}
    phases = plan.get("phases", [])
    st_map = {st["sub_task_id"]: st for st in task.get("sub_tasks", [])}

    if not phases:
        # Legacy fallback if plan is missing.
        sub_tasks = sorted(task.get("sub_tasks", []), key=lambda s: s.get("order", 0))
        for sub_task in sub_tasks:
            _run_sub_task(task_id, sub_task, demo_scenario)
    else:
        for phase in phases:
            phase_tasks = [
                st_map[sid] for sid in phase.get("sub_task_ids", []) if sid in st_map
            ]
            if not phase_tasks:
                continue
            _run_phase_tasks(task_id, phase_tasks, demo_scenario)

    db.advance_orchestrator_phase(task_id)


def _run_sub_tasks_parallel(
    task_id: str, sub_tasks: list[dict[str, Any]], demo_scenario: str,
) -> None:
    """Fan out one thread per sub-task; each waits for a build slot before running.

    Every sub-task gets a thread immediately, but the per-type Jenkins-aware
    capacity gate in `_run_sub_task` limits how many actually build at once
    (2 microservice + 1 portal, minus anything already running in Jenkins). The
    rest sit in the queue. Blocks until the whole phase finishes.
    """
    threads: list[threading.Thread] = []
    for sub_task in sub_tasks:
        t = threading.Thread(
            target=_run_sub_task,
            args=(task_id, sub_task, demo_scenario),
            daemon=True,
            name=f"orchestrator-{sub_task.get('sub_task_id', '')}",
        )
        threads.append(t)
        t.start()
    for t in threads:
        t.join()


def run_task_async(task_id: str, demo_scenario: str = "") -> None:
    threading.Thread(target=run_task, args=(task_id, demo_scenario), daemon=True).start()


def retry_sub_task_async(task_id: str, sub_task_id: str, demo_scenario: str = "") -> None:
    """Re-run a single failed sub-task's build agent (manual DevOps retry).

    Runs on its own thread through the same Jenkins-aware slot gate as a normal
    build, so a manual retry still queues behind other running builds. The new
    build is recorded as a fresh attempt in ``build_attempts`` (the graph gets an
    incremented retry_count so even the mock build number differs from the last).
    """
    demo_scenario = demo_scenario or os.getenv("ORCHESTRATOR_DEMO_SCENARIO", "").strip()

    def _job() -> None:
        st = db.get_sub_task(sub_task_id)
        if not st:
            return
        next_attempt = int(st.get("retry_count", 0)) + 1
        db.reset_sub_task_for_retry(sub_task_id)
        # Unblock the parent task in the UI while this sub-task re-runs.
        db.advance_orchestrator_phase(task_id)
        st = db.get_sub_task(sub_task_id) or st
        if st.get("section") == "phrases":
            _run_phrases_sub_task(task_id, st, demo_scenario, retry_count=next_attempt)
        elif st.get("section") == "yaml":
            _run_yaml_sub_task(task_id, st, demo_scenario, retry_count=next_attempt)
        else:
            _run_sub_task(task_id, st, demo_scenario, retry_count=next_attempt)
        db.advance_orchestrator_phase(task_id)

    threading.Thread(target=_job, daemon=True).start()
