"""
db.py — File-based "database" for the deployment portal MVP.

Replace this module with a real database later (Postgres/SQLite).
Every function here is written so the swap is mechanical: same function
names, same input/output shapes, just backed by SQL instead of JSON.

Storage shape (db.json):
{
  "tasks": {
    "TASK-20392": {
      "task_id": "TASK-20392",
      "jira_key": "TRB-16996",
      "environment": "INTEG",
      "priority": "Normal",
      "description": "...",
      "requested_by": "A. Sharma",
      "status": "running",          # queued | running | done | blocked | failed
      "created_at": "2026-06-24T14:02:00Z",
      "updated_at": "2026-06-24T14:04:07Z",
      "jobs": ["JOB-1", "JOB-2", "JOB-3", "JOB-4"]
    }
  },
  "jobs": {
    "JOB-3": {
      "job_id": "JOB-3",
      "task_id": "TASK-20392",
      "job_type": "microservice",      # microservice | yaml | db | portal | script
      "agent": "microservice_agent",
      "status": "running",             # queued | running | done | failed
      "order": 3,
      "depends_on": ["JOB-1"],
      "fields": { "service": "AccountService", "merge_url": "...", "branch": "..." },
      "jenkins_job": "Titan-Microservices",
      "jenkins_params": { ... },
      "steps": [
        {"label": "Merge request validated", "status": "done", "ts": "..."},
        {"label": "Merged into target branch", "status": "done", "ts": "..."},
        {"label": "Jenkins build running", "status": "running", "ts": "..."},
        {"label": "Image pushed & deployed", "status": "queued", "ts": null}
      ],
      "logs": ["[14:02:55] Started by upstream orchestrator", "..."],
      "created_at": "...",
      "updated_at": "..."
    }
  },
  "counters": { "task": 20392, "job": 4 }
}
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import orchestrator

DB_PATH = Path(__file__).parent / "db.json"
_LOCK = threading.Lock()

_DEFAULT_DB: dict[str, Any] = {
    "tasks": {},
    "jobs": {},
    "counters": {"task": 20391, "job": 0},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read() -> dict[str, Any]:
    if not DB_PATH.exists():
        _write(_DEFAULT_DB)
        return json.loads(json.dumps(_DEFAULT_DB))
    with DB_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write(data: dict[str, Any]) -> None:
    tmp = DB_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    tmp.replace(DB_PATH)  # atomic on POSIX — avoids half-written db.json


def reset_db() -> None:
    """Wipe everything. Useful for demos."""
    with _LOCK:
        _write(json.loads(json.dumps(_DEFAULT_DB)))


# ──────────────────────────────────────────────────────────────────────────
# ID generation
# ──────────────────────────────────────────────────────────────────────────

def next_task_id(db: dict[str, Any]) -> str:
    db["counters"]["task"] += 1
    return f"TASK-{db['counters']['task']}"


def next_job_id(db: dict[str, Any]) -> str:
    db["counters"]["job"] += 1
    return f"JOB-{db['counters']['job']}"


# ──────────────────────────────────────────────────────────────────────────
# Task operations
# ──────────────────────────────────────────────────────────────────────────

def create_task(
    jira_key: str,
    environment: str,
    priority: str,
    description: str,
    requested_by: str,
    jobs_payload: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    jobs_payload: list of {"job_type": "microservice", "fields": {...}}
    Creates one Task + N Jobs atomically, in orchestrator order.
    Returns the assembled task dict (with jobs expanded).
    """
    with _LOCK:
        db = _read()
        task_id = next_task_id(db)
        now = _now()

        sorted_jobs = orchestrator.execution_order(jobs_payload)

        # Pass 1 — allocate IDs and build type → job_id map for DAG edges
        job_ids: list[str] = []
        jobs_by_type: dict[str, str] = {}
        for job_spec in sorted_jobs:
            job_id = next_job_id(db)
            job_ids.append(job_id)
            jobs_by_type[job_spec["job_type"]] = job_id

        dep_map = orchestrator.compute_dependencies(jobs_by_type)

        # Pass 2 — persist job records
        for idx, job_spec in enumerate(sorted_jobs, start=1):
            job_id = jobs_by_type[job_spec["job_type"]]
            db["jobs"][job_id] = {
                "job_id": job_id,
                "task_id": task_id,
                "job_type": job_spec["job_type"],
                "agent": _agent_for(job_spec["job_type"]),
                "status": "queued",
                "order": idx,
                "depends_on": dep_map.get(job_id, []),
                "fields": job_spec.get("fields", {}),
                "jenkins_job": _jenkins_job_for(job_spec["job_type"]),
                "jenkins_params": _build_jenkins_params(
                    task_id, job_id, job_spec["job_type"], job_spec.get("fields", {}), jira_key, environment
                ),
                "steps": _default_steps(job_spec["job_type"]),
                "logs": [f"[{now}] Job created, queued by orchestrator"],
                "created_at": now,
                "updated_at": now,
            }

        task_record = {
            "task_id": task_id,
            "jira_key": jira_key,
            "environment": environment,
            "priority": priority,
            "description": description,
            "requested_by": requested_by,
            "status": "running",
            "created_at": now,
            "updated_at": now,
            "jobs": job_ids,
        }
        db["tasks"][task_id] = task_record

        # Kick off root nodes (no dependencies) — parallel when multiple qualify
        orchestrator.dispatch_ready(
            db["jobs"],
            task_record,
            now,
            lambda msg: f"[{now}] {msg}",
        )
        task_record["status"] = "running"
        task_record["updated_at"] = now

        _write(db)
        return _expand_task(db, task_id)


def list_tasks(
    status: str | None = None,
    requested_by: str | None = None,
    period: str | None = None,
) -> list[dict[str, Any]]:
    db = _read()
    tasks = list(db["tasks"].values())
    if period:
        cutoff = _period_cutoff(period)
        tasks = [t for t in tasks if _parse_ts(t["created_at"]) >= cutoff]
    if status:
        tasks = [t for t in tasks if t["status"] == status]
    if requested_by:
        tasks = [t for t in tasks if t["requested_by"] == requested_by]
    tasks.sort(key=lambda t: t["created_at"], reverse=True)
    return [_expand_task(db, t["task_id"]) for t in tasks]


def get_task(task_id: str) -> dict[str, Any] | None:
    db = _read()
    if task_id not in db["tasks"]:
        return None
    return _expand_task(db, task_id)


def _expand_task(db: dict[str, Any], task_id: str) -> dict[str, Any]:
    task = dict(db["tasks"][task_id])
    task["jobs"] = [db["jobs"][jid] for jid in task["jobs"] if jid in db["jobs"]]
    return task


# ──────────────────────────────────────────────────────────────────────────
# Job operations
# ──────────────────────────────────────────────────────────────────────────

def get_job(job_id: str) -> dict[str, Any] | None:
    db = _read()
    return db["jobs"].get(job_id)


def update_job_status(job_id: str, status: str, log_line: str | None = None) -> dict[str, Any] | None:
    """
    Update a job's status. When a job finishes (done/failed), automatically
    starts the next queued job in the same task (sequential orchestrator behaviour).
    When all jobs in a task are done, marks the task done.
    """
    with _LOCK:
        db = _read()
        job = db["jobs"].get(job_id)
        if not job:
            return None

        now = _now()
        job["status"] = status
        job["updated_at"] = now
        if log_line:
            job["logs"].append(f"[{now}] {log_line}")

        task = db["tasks"][job["task_id"]]
        task["updated_at"] = now

        if status in ("done", "failed"):
            orchestrator.advance_task(
                db["jobs"],
                task,
                now,
                lambda msg: f"[{now}] {msg}",
            )

        _write(db)
        return job


def append_job_step(job_id: str, step_index: int, status: str) -> dict[str, Any] | None:
    with _LOCK:
        db = _read()
        job = db["jobs"].get(job_id)
        if not job or step_index >= len(job["steps"]):
            return None
        job["steps"][step_index]["status"] = status
        job["steps"][step_index]["ts"] = _now()
        job["updated_at"] = _now()
        _write(db)
        return job


# ──────────────────────────────────────────────────────────────────────────
# History / stats
# ──────────────────────────────────────────────────────────────────────────

def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _period_cutoff(period: str) -> datetime:
    now = datetime.now(timezone.utc)
    if period == "daily":
        return now - timedelta(days=1)
    if period == "monthly":
        return now - timedelta(days=30)
    return now - timedelta(days=7)


def get_stats(period: str = "weekly") -> dict[str, Any]:
    db = _read()
    cutoff = _period_cutoff(period)
    tasks = [t for t in db["tasks"].values() if _parse_ts(t["created_at"]) >= cutoff]
    total = len(tasks)
    resolved = sum(1 for t in tasks if t["status"] == "done")
    in_progress = sum(1 for t in tasks if t["status"] in ("running", "queued"))
    blocked = sum(1 for t in tasks if t["status"] in ("blocked", "failed"))
    return {
        "period": period,
        "total": total,
        "resolved": resolved,
        "in_progress": in_progress,
        "blocked": blocked,
    }


# ──────────────────────────────────────────────────────────────────────────
# Static config — mirrors your real jobs.yaml / agent_catalog.yaml
# In production these come from config files, not hardcoded dicts.
# ──────────────────────────────────────────────────────────────────────────

_AGENT_MAP = {
    "microservice": "microservice_agent",
    "yaml": "yaml_automation_agent",
    "db": "liquibase_agent",
    "portal": "portal_agent",
    "script": "utilities_agent",
}

_JENKINS_JOB_MAP = {
    "microservice": "Titan-Microservices",
    "yaml": "yml_automation_2.0",
    "db": "Liquibase",
    "portal": "Titan-Portals",
    "script": "Titan-Utilities",
}

_STEP_TEMPLATES = {
    "microservice": [
        "Merge request validated",
        "Merged into target branch",
        "Jenkins build running",
        "Image pushed & deployed",
    ],
    "yaml": [
        "Config file fetched",
        "YAML syntax validated",
        "Jenkins config job running",
        "Config applied to environment",
    ],
    "db": [
        "Liquibase changeset fetched",
        "SQL guardrail check (no DROP/TRUNCATE)",
        "Liquibase update running",
        "Migration applied",
    ],
    "portal": [
        "Portal build started",
        "Static assets compiled",
        "Jenkins deploy running",
        "Portal live on environment",
    ],
    "script": [
        "Script fetched",
        "Pre-checks passed",
        "Script executing",
        "Completed",
    ],
}


def _agent_for(job_type: str) -> str:
    return _AGENT_MAP.get(job_type, "orchestrator")


def _jenkins_job_for(job_type: str) -> str:
    return _JENKINS_JOB_MAP.get(job_type, "Unknown-Job")


def _default_steps(job_type: str) -> list[dict[str, Any]]:
    labels = _STEP_TEMPLATES.get(job_type, ["Started", "Running", "Finishing", "Done"])
    return [
        {"label": labels[0], "status": "running", "ts": _now()},
        {"label": labels[1], "status": "queued", "ts": None},
        {"label": labels[2], "status": "queued", "ts": None},
        {"label": labels[3], "status": "queued", "ts": None},
    ]


def _build_jenkins_params(
    task_id: str,
    job_id: str,
    job_type: str,
    fields: dict[str, Any],
    jira_key: str,
    environment: str,
) -> dict[str, Any]:
    """Mirrors deployment_platform/jenkins_params.py shape — same keys, structured input."""
    base = {
        "Environment": environment,
        "JIRA_KEY": jira_key,
        "TASK_ID": task_id,
        "STEP_ID": f"{job_type}:{job_id}",
        "RELEASE_TAG": "auto-generated-pending-approval",
    }
    if job_type == "microservice":
        base.update({
            "Service": fields.get("service", ""),
            "MergeID": fields.get("merge_url", ""),
            "Branch": fields.get("branch", ""),
        })
    elif job_type == "yaml":
        base.update({"ConfigFiles": fields.get("config_files", "")})
    elif job_type == "db":
        base.update({
            "ScriptUrl": fields.get("script_url", ""),
            "RunOrder": fields.get("run_order", "Before microservice"),
        })
    elif job_type == "portal":
        base.update({"Portal": fields.get("portal_name", "")})
    elif job_type == "script":
        base.update({"Utility": fields.get("utility_name", "")})
    return base
