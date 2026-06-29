"""
db.py — File-based "database" for the deployment portal MVP (v2).

Jobs represent sections (build / yaml / db), each with a list of
{sub_type, url, label} links pasted by the user.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / "db.json"
_LOCK = threading.Lock()

_DEFAULT_DB: dict[str, Any] = {
    "tasks": {},
    "jobs": {},
    "counters": {"task": 20391, "job": 0},
}

SECTION_ALLOWED_SUBTYPES = {
    "build": {"microservice", "portal", "utility"},
    "yaml": {"microservice", "portal"},
    "db": {"microservice"},
}

SECTION_ORDER = {"yaml": 1, "db": 2, "build": 3}

_AGENT_MAP = {
    "build": "build_agent",
    "yaml": "yaml_automation_agent",
    "db": "liquibase_agent",
}

_STEP_TEMPLATES = {
    "build": ["Links validated", "Merged into target branch", "Jenkins build running", "Deployed"],
    "yaml": ["Links validated", "Config fetched", "Jenkins config job running", "Applied to environment"],
    "db": ["Links validated", "Liquibase changeset fetched", "Liquibase update running", "Migration applied"],
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
    tmp.replace(DB_PATH)


def reset_db() -> None:
    with _LOCK:
        _write(json.loads(json.dumps(_DEFAULT_DB)))


def next_task_id(db: dict[str, Any]) -> str:
    db["counters"]["task"] += 1
    return f"TASK-{db['counters']['task']}"


def next_job_id(db: dict[str, Any]) -> str:
    db["counters"]["job"] += 1
    return f"JOB-{db['counters']['job']}"


class ValidationError(Exception):
    pass


def validate_section_links(section: str, links: list[dict[str, Any]]) -> None:
    allowed = SECTION_ALLOWED_SUBTYPES.get(section)
    if allowed is None:
        raise ValidationError(f"Unknown section '{section}'")
    if not links:
        raise ValidationError(f"Section '{section}' needs at least one link")
    for link in links:
        sub_type = link.get("sub_type")
        if sub_type not in allowed:
            raise ValidationError(
                f"Section '{section}' does not allow sub_type '{sub_type}'. Allowed: {sorted(allowed)}"
            )
        if not link.get("url", "").strip():
            raise ValidationError(f"Every link in '{section}' needs a URL")


def create_task(
    environment: str,
    jira_id: str,
    description: str,
    branch_from: str,
    branch_to: str,
    approver_key: str,
    requested_by: str,
    sections_payload: list[dict[str, Any]],
) -> dict[str, Any]:
    if not sections_payload:
        raise ValidationError("At least one section (Build / YAML / DB) is required")

    for sec in sections_payload:
        validate_section_links(sec["section"], sec["links"])

    with _LOCK:
        db = _read()
        task_id = next_task_id(db)
        now = _now()

        sorted_sections = sorted(
            sections_payload,
            key=lambda s: SECTION_ORDER.get(s["section"], 99),
        )

        job_ids: list[str] = []
        for idx, sec in enumerate(sorted_sections, start=1):
            job_id = next_job_id(db)
            job_ids.append(job_id)
            section = sec["section"]
            db["jobs"][job_id] = {
                "job_id": job_id,
                "task_id": task_id,
                "section": section,
                "agent": _AGENT_MAP.get(section, "orchestrator"),
                "status": "queued",
                "order": idx,
                "links": sec["links"],
                "steps": _default_steps(section, queued=True),
                "logs": [f"[{now}] Job created, awaiting approval before orchestrator starts"],
                "created_at": now,
                "updated_at": now,
            }

        db["tasks"][task_id] = {
            "task_id": task_id,
            "environment": environment,
            "jira_id": jira_id,
            "description": description,
            "branch_from": branch_from,
            "branch_to": branch_to,
            "approver_key": approver_key,
            "requested_by": requested_by,
            "status": "pending_approval",
            "approver_approved": False,
            "devops_approved": False,
            "approver_approved_at": None,
            "devops_approved_at": None,
            "created_at": now,
            "updated_at": now,
            "jobs": job_ids,
        }

        _write(db)
        return _expand_task(db, task_id)


def list_tasks(
    status: str | None = None,
    requested_by: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    db = _read()
    tasks = list(db["tasks"].values())
    if status:
        tasks = [t for t in tasks if t["status"] == status]
    if requested_by:
        tasks = [t for t in tasks if t["requested_by"] == requested_by]
    tasks.sort(key=lambda t: t["created_at"], reverse=True)
    if limit:
        tasks = tasks[:limit]
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


def get_job(job_id: str) -> dict[str, Any] | None:
    db = _read()
    return db["jobs"].get(job_id)


def update_job_status(
    job_id: str,
    status: str,
    log_line: str | None = None,
) -> dict[str, Any] | None:
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
            _advance_orchestrator(db, task)

        _write(db)
        return job


def approve_task(task_id: str, role: str) -> dict[str, Any]:
    if role not in ("approver", "devops"):
        raise ValidationError(f"Invalid approval role '{role}'. Use 'approver' or 'devops'.")

    with _LOCK:
        db = _read()
        if task_id not in db["tasks"]:
            raise ValidationError(f"Task {task_id} not found")

        task = db["tasks"][task_id]
        if task["status"] not in ("pending_approval", "running"):
            raise ValidationError(f"Task {task_id} cannot be approved in status '{task['status']}'")

        now = _now()
        if role == "approver":
            if task.get("approver_approved"):
                raise ValidationError("Approver has already approved this request")
            task["approver_approved"] = True
            task["approver_approved_at"] = now
        else:
            if task.get("devops_approved"):
                raise ValidationError("DevOps has already approved this request")
            task["devops_approved"] = True
            task["devops_approved_at"] = now

        task["updated_at"] = now

        if task.get("approver_approved") and task.get("devops_approved"):
            _start_orchestrator(db, task)

        _write(db)
        return _expand_task(db, task_id)


def _start_orchestrator(db: dict[str, Any], task: dict[str, Any]) -> None:
    jobs = [db["jobs"][jid] for jid in task["jobs"]]
    jobs.sort(key=lambda j: j["order"])
    now = _now()

    if not jobs:
        task["status"] = "done"
        return

    first = jobs[0]
    first["status"] = "running"
    first["updated_at"] = now
    first["logs"].append(f"[{now}] Both approvals received — orchestrator started")
    if first.get("steps"):
        first["steps"][0]["status"] = "running"
        first["steps"][0]["ts"] = now

    task["status"] = "running"


def _advance_orchestrator(db: dict[str, Any], task: dict[str, Any]) -> None:
    if task["status"] == "pending_approval":
        return

    jobs = [db["jobs"][jid] for jid in task["jobs"]]
    jobs.sort(key=lambda j: j["order"])

    if any(j["status"] == "failed" for j in jobs):
        task["status"] = "blocked"
        return

    next_job = next((j for j in jobs if j["status"] == "queued"), None)
    if next_job:
        next_job["status"] = "running"
        next_job["updated_at"] = _now()
        next_job["logs"].append(f"[{_now()}] Orchestrator dispatched to {next_job['agent']}")
        if next_job.get("steps"):
            next_job["steps"][0]["status"] = "running"
            next_job["steps"][0]["ts"] = _now()
        task["status"] = "running"
    else:
        task["status"] = "done"


def _default_steps(section: str, queued: bool = False) -> list[dict[str, Any]]:
    labels = _STEP_TEMPLATES.get(section, ["Started", "Running", "Finishing", "Done"])
    first_status = "queued" if queued else "running"
    return [
        {"label": labels[0], "status": first_status, "ts": None if queued else _now()},
        {"label": labels[1], "status": "queued", "ts": None},
        {"label": labels[2], "status": "queued", "ts": None},
        {"label": labels[3], "status": "queued", "ts": None},
    ]


def get_stats(period: str = "weekly") -> dict[str, Any]:
    db = _read()
    tasks = list(db["tasks"].values())
    total = len(tasks)
    resolved = sum(1 for t in tasks if t["status"] == "done")
    pending = sum(1 for t in tasks if t["status"] == "pending_approval")
    in_progress = sum(1 for t in tasks if t["status"] in ("running", "queued"))
    blocked = sum(1 for t in tasks if t["status"] in ("blocked", "failed"))
    return {
        "period": period,
        "total": total,
        "resolved": resolved,
        "pending": pending,
        "in_progress": in_progress,
        "blocked": blocked,
    }


def get_recent_activity(limit: int = 6) -> list[dict[str, Any]]:
    tasks = list_tasks(limit=limit)
    out = []
    for t in tasks:
        out.append({
            "task_id": t["task_id"],
            "jira_id": t["jira_id"],
            "environment": t["environment"],
            "status": t["status"],
            "requested_by": t["requested_by"],
            "created_at": t["created_at"],
            "sections": [j["section"] for j in t["jobs"]],
        })
    return out
