"""
orchestrator.py — DAG dependency rules and dispatch logic.

Today: lightweight in-process graph evaluation (file DB).
Later: swap dispatch for Temporal / Prefect / custom worker pool —
        dependency graph and job state shape stay the same.
"""

from __future__ import annotations

from typing import Any

# Default upstream job types each job type waits on (within the same task).
# Multiple entries at the same level can run in parallel once deps are done.
_DAG_RULES: dict[str, list[str]] = {
    "script": [],
    "yaml": ["script"],
    "db": ["yaml", "script"],       # yaml preferred; script fallback if no yaml job
    "microservice": ["yaml", "db"],
    "portal": ["microservice"],
}


def compute_dependencies(jobs_by_type: dict[str, str]) -> dict[str, list[str]]:
    """
    Given {job_type: job_id} for one task, return {job_id: [depends_on_job_id, ...]}.
    Only links to jobs that actually exist in this task.
    """
    deps: dict[str, list[str]] = {}
    for job_type, job_id in jobs_by_type.items():
        upstream_types = _DAG_RULES.get(job_type, [])
        upstream_ids: list[str] = []
        for ut in upstream_types:
            if ut in jobs_by_type:
                upstream_ids.append(jobs_by_type[ut])
                break  # first matching upstream type wins (yaml before script for db)
        # microservice needs ALL present yaml + db jobs
        if job_type == "microservice":
            upstream_ids = [jobs_by_type[t] for t in ("yaml", "db") if t in jobs_by_type]
        elif job_type == "portal":
            if "microservice" in jobs_by_type:
                upstream_ids = [jobs_by_type["microservice"]]
            else:
                upstream_ids = list(jobs_by_type.values())
                upstream_ids.remove(job_id)
        deps[job_id] = upstream_ids
    return deps


def ready_jobs(jobs: list[dict[str, Any]], jobs_index: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Jobs that are queued and have every dependency in 'done' state."""
    ready: list[dict[str, Any]] = []
    for job in jobs:
        if job["status"] != "queued":
            continue
        deps = job.get("depends_on") or []
        if all(jobs_index[d]["status"] == "done" for d in deps if d in jobs_index):
            ready.append(job)
    return ready


def dispatch_ready(db_jobs: dict[str, dict[str, Any]], task: dict[str, Any], now: str, log_fn) -> None:
    """Start every job whose dependencies are satisfied (supports parallel branches)."""
    jobs = [db_jobs[jid] for jid in task["jobs"]]
    jobs_index = {j["job_id"]: j for j in jobs}

    for job in ready_jobs(jobs, jobs_index):
        job["status"] = "running"
        job["updated_at"] = now
        job["logs"].append(log_fn(f"Orchestrator dispatched to {job['agent']}"))


def advance_task(db_jobs: dict[str, dict[str, Any]], task: dict[str, Any], now: str, log_fn) -> None:
    """
    Re-evaluate the DAG after a job completes or fails.
    - Any failure → task blocked
    - Ready queued jobs → start (possibly several in parallel)
    - All done → task done
    """
    jobs = [db_jobs[jid] for jid in task["jobs"]]

    if any(j["status"] == "failed" for j in jobs):
        task["status"] = "blocked"
        return

    if all(j["status"] == "done" for j in jobs):
        task["status"] = "done"
        return

    dispatch_ready(db_jobs, task, now, log_fn)

    if any(j["status"] == "running" for j in jobs):
        task["status"] = "running"
    elif any(j["status"] == "queued" for j in jobs):
        task["status"] = "running"  # waiting on in-flight deps
    else:
        task["status"] = "done"


def execution_order(jobs_payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Topological sort for display — same priority as orchestration_plan.py."""
    order_map = {"script": 1, "yaml": 2, "db": 3, "microservice": 4, "portal": 5}
    return sorted(jobs_payload, key=lambda j: order_map.get(j["job_type"], 99))
