"""
db.py — File-based "database" for the deployment portal (v3).

Jobs represent sections (yaml / db / phrases / build). Each section has
an optional release_branch and a list of links. Links now reference a
catalog service via service_key (dropdown selection) instead of a pasted
URL.

Approval is a sequential, conditional chain:
    Code Freeze OFF:  dev_lead -> devops
    Code Freeze ON:   dev_lead -> qa -> devops
The chain shape is captured on the task at creation time.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

DB_PATH = Path(__file__).parent / "db.json"
_LOCK = threading.Lock()

_DEFAULT_DB: dict[str, Any] = {
    "tasks": {},
    "jobs": {},
    "sub_tasks": {},
    "counters": {"task": 20391, "job": 0, "sub_task": 0},
}

# Per the meeting discussion: YAML and DB both allow Portal + Microservice.
# Phrases mirrors them. Build additionally allows Utilities.
SECTION_ALLOWED_SUBTYPES = {
    "build": {"microservice", "portal", "utility"},
    "yaml": {"microservice", "portal"},
    "db": {"microservice"},
    "phrases": {"portal"},
}

SECTION_ORDER = {"yaml": 1, "db": 2, "phrases": 3, "build": 4}

# Sections that require a release branch (Build does not).
SECTIONS_NEEDING_RELEASE_BRANCH = {"yaml", "db", "phrases"}

_AGENT_MAP = {
    "build": "build_agent",
    "yaml": "yaml_automation_agent",
    "db": "liquibase_agent",
    "phrases": "phrases_agent",
}

_STEP_TEMPLATES = {
    "build": ["Links validated", "Merged into target branch", "Jenkins build running", "Deployed"],
    "yaml": ["Links validated", "Config fetched", "Jenkins config job running", "Applied to environment"],
    "db": ["Links validated", "Liquibase changeset fetched", "Liquibase update running", "Migration applied"],
    "phrases": ["Links validated", "Phrases fetched", "Phrases job running", "Applied to environment"],
}

# ──────────────────────────────────────────────────────────────────────────
# Sub-task model (v4)
#
# A Job is a section container. Sub-Tasks are the atomic units the
# orchestrator actually plans and executes (one merge, one YAML file, one DB
# script, one phrases deploy). See july_2orchestratorPlan.md sections 1–2.
# ──────────────────────────────────────────────────────────────────────────

# Which agent handles each sub-task type.
_SUB_TASK_AGENT = {
    "build": "merge_build_agent",
    "yaml": "yaml_agent",
    "db": "db_agent",
    "phrases": "phrases_agent",
}

# Jenkins job triggered by each sub-task (portal builds use a different job).
_JENKINS_JOB = {
    ("build", "microservice"): "Titan-Microservices",
    ("build", "portal"): "Titan-Portals",
    ("build", "utility"): "Titan-Utilities",
    ("yaml", None): "yml_automation_2.0",
    ("phrases", None): "phrase-deploy",
    ("db", None): "liquibase-runner",
}

# Verbatim step labels per sub-task type (plan §1.2). Do not reorder.
_SUB_TASK_STEP_LABELS = {
    ("build", "microservice"): [
        "Validate MR is mergeable (GitSpace check)",
        "Merge !{mr_id} into {release_branch}",
        "Trigger Jenkins job: {jenkins_job}",
        "Waiting for Jenkins build #{build_number} to complete",
        "Poll API gateway: GET {gateway}/health every 5 min",
        "AI verification: Ollama reads Jenkins logs, confirms success",
    ],
    ("build", "portal"): [
        "Validate MR is mergeable (GitSpace check)",
        "Merge !{mr_id} into {release_branch}",
        "Trigger Jenkins job: Titan-Portals",
        "Waiting for Jenkins build #{build_number} to complete",
        "Poll portal URL for HTTP 200 every 5 min",
        "AI verification: Ollama reads Jenkins logs, confirms success",
    ],
    ("yaml", None): [
        "Validate YAML syntax (parse check)",
        "Validate config file against schema",
        "Trigger Jenkins job: yml_automation_2.0",
        "Waiting for Jenkins build #{build_number} to complete",
        "Verify config applied: GET {config_endpoint}",
        "Mark config propagated",
    ],
    ("db", None): [
        "Validate SQL syntax (no DROP/TRUNCATE/DELETE without WHERE)",
        "Validate Liquibase changeset format",
        "Run Liquibase update",
        "Verify DB migration applied",
        "Run smoke test query",
    ],
    ("phrases", None): [
        "Validate phrase file format",
        "Validate phrases against key schema",
        "Trigger phrase deployment job",
        "Verify phrases propagated to CDN",
    ],
}

# Ordered phases the orchestrator runs (plan §1.3). DB → config/phrases → build.
_PHASE_DEFS = [
    (1, "Database migrations", ["db"]),
    (2, "Config & phrases", ["yaml", "phrases"]),
    (3, "Build & deploy", ["build"]),
]

_MOCK_JENKINS_BASE = 1280


def _stable_seed(*parts: str) -> int:
    """Deterministic, process-independent seed (unlike hash())."""
    return sum(ord(c) for c in "".join(parts))


def _step_labels(section: str, sub_type: str) -> list[str]:
    return _SUB_TASK_STEP_LABELS.get(
        (section, sub_type), _SUB_TASK_STEP_LABELS.get((section, None), [])
    )


def _jenkins_job(section: str, sub_type: str) -> str | None:
    return _JENKINS_JOB.get((section, sub_type), _JENKINS_JOB.get((section, None)))


ApprovalRole = Literal["dev_lead", "qa", "devops"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read() -> dict[str, Any]:
    if not DB_PATH.exists():
        _write(_DEFAULT_DB)
        return json.loads(json.dumps(_DEFAULT_DB))
    with DB_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    # Forward-migrate older DB files that predate the sub-task model.
    data.setdefault("sub_tasks", {})
    data.setdefault("counters", {})
    data["counters"].setdefault("task", 20391)
    data["counters"].setdefault("job", 0)
    data["counters"].setdefault("sub_task", 0)
    return data


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


def next_sub_task_id(db: dict[str, Any]) -> str:
    db["counters"]["sub_task"] += 1
    return f"ST-{db['counters']['sub_task']}"


class ValidationError(Exception):
    pass


class ApprovalError(Exception):
    pass


def validate_section_links(section: str, links: list[dict[str, Any]]) -> None:
    allowed = SECTION_ALLOWED_SUBTYPES.get(section)
    if allowed is None:
        raise ValidationError(f"Unknown section '{section}'")
    if not links:
        raise ValidationError(f"Section '{section}' needs at least one item")
    for link in links:
        sub_type = link.get("sub_type")
        if sub_type not in allowed:
            raise ValidationError(
                f"Section '{section}' does not allow sub_type '{sub_type}'. Allowed: {sorted(allowed)}"
            )
        if not (link.get("service_key") or "").strip():
            raise ValidationError(f"Every item in '{section}' needs a selected service")


# ──────────────────────────────────────────────────────────────────────────
# Approval state machine
# ──────────────────────────────────────────────────────────────────────────

def get_approval_chain(code_freeze_enabled: bool) -> list[str]:
    if code_freeze_enabled:
        return ["dev_lead", "qa", "devops"]
    return ["dev_lead", "devops"]


def get_current_approval_stage(task: dict[str, Any]) -> str | None:
    if task["status"] == "rejected":
        return None
    for role in task["approval_chain"]:
        if task["approvals"].get(role) is None:
            return role
    return None


def apply_approval_decision(
    task_id: str,
    role: str,
    decision: str,
    by: str,
    comment: str | None = None,
) -> dict[str, Any]:
    """Single entry point for all approval actions. Validates turn order,
    requires a comment on reject, updates status, and starts the
    orchestrator when the chain completes."""
    if decision not in ("approved", "rejected"):
        raise ApprovalError(f"Invalid decision '{decision}'")

    with _LOCK:
        db = _read()
        task = db["tasks"].get(task_id)
        if not task:
            raise ApprovalError(f"Task {task_id} not found")

        current = get_current_approval_stage(task)
        if current != role:
            raise ApprovalError(f"It's not {role}'s turn to approve. Current stage: {current}")

        if decision == "rejected" and not (comment and comment.strip()):
            raise ApprovalError("A comment is required when rejecting")

        now = _now()
        task["approvals"][role] = {"decision": decision, "comment": comment, "by": by, "at": now}
        task["updated_at"] = now

        if decision == "rejected":
            task["status"] = "rejected"
            task["rejection_reason"] = comment
            _write(db)
            return _expand_task(db, task_id)

        next_stage = get_current_approval_stage(task)
        if next_stage is None:
            task["status"] = "running"
            jobs = [db["jobs"][jid] for jid in task["jobs"]]
            jobs.sort(key=lambda j: j["order"])
            if jobs:
                first = jobs[0]
                first["status"] = "running"
                first["updated_at"] = now
                first["logs"].append(f"[{now}] All approvals received — orchestrator dispatched to {first['agent']}")
                if first.get("steps"):
                    first["steps"][0]["status"] = "running"
                    first["steps"][0]["ts"] = now
            # Kick off Phase 1 sub-tasks (the orchestrator executes sub-tasks).
            _reconcile_sub_task_orchestration(db, task)
        else:
            task["status"] = "pending_approval"

        _write(db)
        return _expand_task(db, task_id)


# ──────────────────────────────────────────────────────────────────────────
# Sub-task orchestration & mutation
# ──────────────────────────────────────────────────────────────────────────

def _sub_tasks_of(db: dict[str, Any], task: dict[str, Any]) -> list[dict[str, Any]]:
    return [db["sub_tasks"][sid] for sid in task.get("sub_tasks", []) if sid in db["sub_tasks"]]


def _start_phase(db: dict[str, Any], phase: dict[str, Any], now: str) -> None:
    for sid in phase["sub_task_ids"]:
        st = db["sub_tasks"].get(sid)
        if st and st["status"] == "queued":
            st["status"] = "running"
            st["updated_at"] = now
            st["logs"].append(f"[{now}] Orchestrator dispatched to {st['agent']}")
            if st.get("steps"):
                st["steps"][0]["status"] = "running"
                st["steps"][0]["ts"] = now


def _reconcile_sub_task_orchestration(db: dict[str, Any], task: dict[str, Any]) -> None:
    """Advance the phase machine: start the next phase when the current one is
    fully done; block the task if any sub-task failed."""
    plan = task.get("orchestrator_plan") or {}
    phases = plan.get("phases", [])
    if not phases:
        return

    now = _now()
    all_sts = _sub_tasks_of(db, task)

    # A failed sub-task blocks its phase and the whole task.
    if any(s["status"] == "failed" for s in all_sts):
        task["status"] = "blocked"
        task["updated_at"] = now
        return

    for phase in phases:
        phase_sts = [db["sub_tasks"][sid] for sid in phase["sub_task_ids"] if sid in db["sub_tasks"]]
        if not phase_sts:
            continue
        if all(s["status"] == "done" for s in phase_sts):
            continue
        # First phase that is not fully done: start it if idle, else it's in flight.
        if not any(s["status"] == "running" for s in phase_sts):
            _start_phase(db, phase, now)
        task["status"] = "running"
        task["updated_at"] = now
        return

    # Every phase complete.
    task["status"] = "done"
    task["updated_at"] = now


def get_sub_task(sub_task_id: str) -> dict[str, Any] | None:
    db = _read()
    return db["sub_tasks"].get(sub_task_id)


def get_orchestrator_plan(task_id: str) -> dict[str, Any] | None:
    db = _read()
    task = db["tasks"].get(task_id)
    if not task:
        return None
    return task.get("orchestrator_plan")


def get_task_with_sub_tasks(task_id: str) -> dict[str, Any] | None:
    db = _read()
    if task_id not in db["tasks"]:
        return None
    task = _expand_task(db, task_id)
    task["sub_tasks"] = _sub_tasks_of(db, db["tasks"][task_id])
    task["orchestrator_plan"] = db["tasks"][task_id].get("orchestrator_plan")
    return task


def update_sub_task_status(
    sub_task_id: str, status: str, log_line: str | None = None
) -> dict[str, Any] | None:
    with _LOCK:
        db = _read()
        st = db["sub_tasks"].get(sub_task_id)
        if not st:
            return None
        now = _now()
        st["status"] = status
        st["updated_at"] = now
        if log_line:
            st["logs"].append(f"[{now}] {log_line}")
        _write(db)
        return st


def advance_sub_task_step(
    sub_task_id: str, step_id: str, status: str, detail: str | None = None
) -> dict[str, Any] | None:
    with _LOCK:
        db = _read()
        st = db["sub_tasks"].get(sub_task_id)
        if not st:
            return None
        now = _now()
        for step in st["steps"]:
            if step["step_id"] == step_id:
                step["status"] = status
                step["detail"] = detail
                step["ts"] = now
                break
        st["updated_at"] = now
        _write(db)
        return st


def update_sub_task_after_tick(
    sub_task_id: str, steps: list[dict[str, Any]], status: str, detail: str | None = None
) -> dict[str, Any] | None:
    """Persist the mutated steps + status produced by mock_executor.tick_sub_task."""
    with _LOCK:
        db = _read()
        st = db["sub_tasks"].get(sub_task_id)
        if not st:
            return None
        now = _now()
        st["steps"] = steps
        st["status"] = status
        st["updated_at"] = now
        if detail:
            st["logs"].append(f"[{now}] {detail}")
        _write(db)
        return st


def advance_orchestrator_phase(task_id: str) -> dict[str, Any] | None:
    """Re-evaluate the phase machine after a sub-task completes or fails."""
    with _LOCK:
        db = _read()
        task = db["tasks"].get(task_id)
        if not task:
            return None
        _reconcile_sub_task_orchestration(db, task)
        _write(db)
        return _expand_task(db, task_id)


# ──────────────────────────────────────────────────────────────────────────
# Task operations
# ──────────────────────────────────────────────────────────────────────────

def _make_sub_task_steps(section: str, sub_type: str, ctx: dict[str, Any]) -> list[dict[str, Any]]:
    steps = []
    for i, raw in enumerate(_step_labels(section, sub_type), start=1):
        try:
            label = raw.format(**ctx)
        except Exception:
            label = raw
        steps.append({"step_id": f"s{i}", "label": label, "status": "queued", "detail": None, "ts": None})
    return steps


def _build_sub_task(
    db: dict[str, Any],
    task_id: str,
    job_id: str,
    section: str,
    sub_type: str,
    service_key: str,
    label: str,
    release_branch: str,
) -> str:
    sub_task_id = next_sub_task_id(db)
    now = _now()
    seed_key = service_key or label or sub_task_id
    build_number = _MOCK_JENKINS_BASE + _stable_seed(seed_key) % 100
    mr_id = 2800 + _stable_seed(seed_key) % 200
    jenkins_job = _jenkins_job(section, sub_type)
    ctx = {
        "mr_id": mr_id,
        "release_branch": release_branch or "release branch",
        "jenkins_job": jenkins_job or "jenkins",
        "build_number": build_number,
        "gateway": f"https://api.titan.internal/{service_key or 'svc'}",
        "config_endpoint": f"https://config.titan.internal/{service_key or 'svc'}",
    }

    if section == "build":
        jenkins_params = {"Service": label, "MergeID": f"!{mr_id}", "ReleaseBranch": release_branch or ""}
    elif section == "yaml":
        jenkins_params = {"ConfigFile": label, "Environment": "auto"}
    elif section == "db":
        jenkins_params = {"Changelog": service_key, "Mode": "update"}
    elif section == "phrases":
        jenkins_params = {"Service": label, "Regions": "3"}
    else:
        jenkins_params = {}

    db["sub_tasks"][sub_task_id] = {
        "sub_task_id": sub_task_id,
        "job_id": job_id,
        "task_id": task_id,
        "section": section,
        "sub_type": sub_type,
        "service_key": service_key,
        "label": label,
        "release_branch": release_branch or None,
        "order": 0,  # assigned by _build_orchestrator_plan
        "status": "queued",
        "agent": _SUB_TASK_AGENT.get(section, "orchestrator"),
        "steps": _make_sub_task_steps(section, sub_type, ctx),
        "logs": [],
        "jenkins_job": jenkins_job,
        "jenkins_params": jenkins_params,
        "mock_build_number": build_number,
        "created_at": now,
        "updated_at": now,
    }
    return sub_task_id


def _build_orchestrator_plan(db: dict[str, Any], sub_task_ids: list[str]) -> dict[str, Any]:
    sts = [db["sub_tasks"][sid] for sid in sub_task_ids]
    phases: list[dict[str, Any]] = []
    order_counter = 0
    for phase_num, label, sections in _PHASE_DEFS:
        ids = [st["sub_task_id"] for st in sts if st["section"] in sections]
        if not ids:
            continue
        for sid in ids:
            order_counter += 1
            db["sub_tasks"][sid]["order"] = order_counter
        phases.append({"phase": phase_num, "label": label, "parallel": True, "sub_task_ids": ids})
    total = len(sub_task_ids)
    return {
        "phases": phases,
        "total_sub_tasks": total,
        "estimated_minutes": (10 + total * 6) if total else 0,
    }


def create_task(
    environment: str,
    jira_id: str,
    description: str,
    branch_from: str,
    branch_to: str,
    approver_key: str,
    requested_by: str,
    sections_payload: list[dict[str, Any]],
    code_freeze_enabled: bool = False,
    branches: list[dict[str, str]] | None = None,
    cc_emails: str = "",
) -> dict[str, Any]:
    if not sections_payload:
        raise ValidationError("At least one section (YAML / DB / Phrases / Build) is required")

    for sec in sections_payload:
        validate_section_links(sec["section"], sec["links"])
        if sec["section"] in SECTIONS_NEEDING_RELEASE_BRANCH and not (sec.get("release_branch") or "").strip():
            raise ValidationError(f"Release branch is required for the {sec['section']} section")

    with _LOCK:
        db = _read()
        task_id = next_task_id(db)
        now = _now()

        sorted_sections = sorted(
            sections_payload,
            key=lambda s: SECTION_ORDER.get(s["section"], 99),
        )

        job_ids: list[str] = []
        sub_task_ids: list[str] = []
        for idx, sec in enumerate(sorted_sections, start=1):
            job_id = next_job_id(db)
            job_ids.append(job_id)
            section = sec["section"]
            db["jobs"][job_id] = {
                "job_id": job_id,
                "task_id": task_id,
                "section": section,
                "release_branch": sec.get("release_branch", ""),
                "agent": _AGENT_MAP.get(section, "orchestrator"),
                "status": "queued",
                "order": idx,
                "branch_from": (sec.get("branch_from") or "").strip(),
                "branch_to": (sec.get("branch_to") or "").strip(),
                "links": sec["links"],
                "steps": _default_steps(section, queued=True),
                "logs": [f"[{now}] Job created, awaiting approval before orchestrator starts"],
                "created_at": now,
                "updated_at": now,
            }

            # Each link in a section becomes one atomic sub-task.
            release_branch = sec.get("release_branch") or sec.get("branch_to") or ""
            for link in sec["links"]:
                sid = _build_sub_task(
                    db,
                    task_id=task_id,
                    job_id=job_id,
                    section=section,
                    sub_type=link.get("sub_type", ""),
                    service_key=link.get("service_key", ""),
                    label=link.get("label") or link.get("service_key", ""),
                    release_branch=release_branch,
                )
                sub_task_ids.append(sid)

        orchestrator_plan = _build_orchestrator_plan(db, sub_task_ids)

        chain = get_approval_chain(code_freeze_enabled)
        db["tasks"][task_id] = {
            "task_id": task_id,
            "environment": environment,
            "jira_id": jira_id,
            "description": description,
            "branch_from": branch_from,
            "branch_to": branch_to,
            "branches": branches if branches else ([{"from": branch_from, "to": branch_to}] if (branch_from or branch_to) else []),
            "approver_key": approver_key,
            "cc_emails": cc_emails,
            "requested_by": requested_by,
            "status": "pending_approval",
            "code_freeze_enabled": code_freeze_enabled,
            "approval_chain": chain,
            "approvals": {role: None for role in chain},
            "rejection_reason": None,
            "validation_status": "pending",
            "validation_report": None,
            "created_at": now,
            "updated_at": now,
            "jobs": job_ids,
            "sub_tasks": sub_task_ids,
            "orchestrator_plan": orchestrator_plan,
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
    task["current_stage"] = get_current_approval_stage(db["tasks"][task_id])
    return task


def set_validation_status(task_id: str, status: str) -> None:
    with _LOCK:
        db = _read()
        task = db["tasks"].get(task_id)
        if not task:
            return
        task["validation_status"] = status
        task["updated_at"] = _now()
        _write(db)


def set_validation_report(task_id: str, report: dict[str, Any]) -> dict[str, Any] | None:
    with _LOCK:
        db = _read()
        task = db["tasks"].get(task_id)
        if not task:
            return None
        task["validation_report"] = report
        task["validation_status"] = "ready"
        task["updated_at"] = _now()
        _write(db)
        return _expand_task(db, task_id)


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


def _advance_orchestrator(db: dict[str, Any], task: dict[str, Any]) -> None:
    if task["status"] in ("pending_approval", "rejected"):
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
    blocked = sum(1 for t in tasks if t["status"] in ("blocked", "failed", "rejected"))
    return {
        "period": period,
        "total": total,
        "resolved": resolved,
        "pending": pending,
        "in_progress": in_progress,
        "blocked": blocked,
        "rejected": sum(1 for t in tasks if t["status"] == "rejected"),
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
