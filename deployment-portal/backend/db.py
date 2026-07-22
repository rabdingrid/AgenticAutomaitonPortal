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
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from orchestrator import jenkins_params
import catalog
import gitspace


def _orchestrator_mode() -> str:
    return os.getenv("ORCHESTRATOR_MODE", "mock").lower().strip()

DB_PATH = Path(__file__).parent / "db.json"
_LOCK = threading.Lock()

_DEFAULT_DB: dict[str, Any] = {
    "tasks": {},
    "jobs": {},
    "sub_tasks": {},
    "counters": {"task": 20391, "job": 0, "sub_task": 0},
}

# Per the meeting discussion: YAML and DB both allow Portal + Microservice.
# Phrases has three artifact kinds (Phrases / SchemaForms / NewSchemaForms), all portal-scoped.
SECTION_ALLOWED_SUBTYPES = {
    "build": {"microservice", "portal", "utility"},
    "yaml": {"microservice", "portal"},
    "db": {"microservice"},
    "phrases": {"phrases", "schemaforms", "newschemaforms"},
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
    ("yaml", None): "YML_Automation_V3",
    ("phrases", None): "Json_Automation_V2",
    ("db", None): "DB-Script-Automation-Liquibase",
}

# Verbatim step labels per sub-task type (plan §1.2). Do not reorder.
_SUB_TASK_STEP_LABELS = {
    ("build", "microservice"): [
        "Validate MR is mergeable (GitSpace check)",
        "Merge !{mr_id} into {release_branch}",
        "Trigger Jenkins job: {jenkins_job}",
        "Wait for Jenkins build to complete",
        "Health check: poll API gateway",
        "Post-build verification",
    ],
    ("build", "portal"): [
        "Validate MR is mergeable (GitSpace check)",
        "Merge !{mr_id} into {release_branch}",
        "Trigger Jenkins job: Titan-Portals",
        "Wait for Jenkins build to complete",
        "Health check: poll portal URL",
        "Post-build verification",
    ],
    ("yaml", None): [
        "Validate YAML syntax (parse check)",
        "Validate YAML formatting and indentation",
        "Trigger Jenkins job: YML_Automation_V3",
        "Waiting for Jenkins build #{build_number} to complete",
        "Verify config applied: GET {config_endpoint}",
        "Mark config propagated",
    ],
    ("db", None): [
        "Validate SQL syntax (no DROP/TRUNCATE/DELETE without WHERE)",
        "Validate Liquibase changeset format",
        "Trigger Jenkins job: DB-Script-Automation-Liquibase",
        "Wait for Jenkins build #{build_number} to complete",
        "Verify DB migration applied",
        "Run smoke test query",
    ],
    ("phrases", None): [
        "Validate JSON format (syntax check)",
        "Build Jenkins deployment parameters",
        "Trigger Jenkins job: Json_Automation_V2",
        "Wait for Jenkins build #{build_number} to complete",
        "Verify phrases propagated to CDN",
    ],
}

# Ordered phases the orchestrator runs (plan §1.3). DB → config/phrases → build.
_PHASE_DEFS = [
    (1, "Database migrations", ["db"]),
    (2, "YAML & Json & SchemaForms", ["yaml", "phrases"]),
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


_PERIOD_DAYS: dict[str, int] = {"daily": 1, "weekly": 7, "monthly": 30}


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _task_within_period(task: dict[str, Any], period: str | None) -> bool:
    if not period:
        return True
    days = _PERIOD_DAYS.get(period)
    if days is None:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return _parse_ts(task["created_at"]) >= cutoff


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


def _task_needs_release_tag(db: dict[str, Any], task: dict[str, Any]) -> bool:
    """Sub-tasks that trigger Jenkins need RELEASE_TAG."""
    for sid in task.get("sub_tasks", []):
        st = db["sub_tasks"].get(sid)
        if not st:
            continue
        if st["section"] == "build" and st.get("sub_type") in ("microservice", "portal"):
            return True
        if st["section"] in ("yaml", "db", "phrases"):
            return True
    return False


def _refresh_build_jenkins_params(db: dict[str, Any], task: dict[str, Any]) -> None:
    """Rebuild Jenkins params for microservice and portal builds after DevOps sets RELEASE_TAG."""
    release_tag = (task.get("release_tag") or "").strip()
    for sid in task.get("sub_tasks", []):
        st = db["sub_tasks"].get(sid)
        if not st or st["section"] != "build":
            continue
        sub_type = st.get("sub_type")
        if sub_type == "microservice":
            st["jenkins_params"] = jenkins_params.build_titan_microservices_params(
                service_label=st["label"],
                release_tag=release_tag,
            )
            st["jenkins_job"] = jenkins_params.microservices_job_path()
        elif sub_type == "portal":
            st["jenkins_params"] = jenkins_params.build_titan_portals_params(
                portal_label=st["label"],
                release_tag=release_tag,
            )
            st["jenkins_job"] = jenkins_params.portals_job_path()
        else:
            continue
        st["updated_at"] = _now()


def _extract_merge_requests_from_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    mrs: list[dict[str, Any]] = []
    for g in report.get("sections") or []:
        if g.get("section") != "build":
            continue
        for it in g.get("items") or []:
            m = it.get("merge") or {}
            iid = int(m.get("iid") or 0)
            if not iid:
                continue
            urls = it.get("urls") or {}
            mrs.append({
                "service_key": it.get("service_key", ""),
                "label": it.get("label", ""),
                "project": m.get("project", ""),
                "iid": iid,
                "source_branch": m.get("source_branch", ""),
                "target_branch": m.get("target_branch", ""),
                "state": m.get("state", "opened"),
                "mergeable": m.get("mergeable"),
                "has_conflicts": bool(m.get("has_conflicts")),
                "web_url": m.get("web_url") or urls.get("merge_request") or urls.get("gitspace", ""),
                "changes_summary": m.get("changes_summary", ""),
                "files_changed": m.get("files_changed") or [],
            })
    return mrs


def _sync_subtask_merge_ids(db: dict[str, Any], task: dict[str, Any], mrs: list[dict[str, Any]]) -> None:
    by_key: dict[str, dict[str, Any]] = {}
    for mr in mrs:
        key = f"{mr['service_key']}:{mr['source_branch']}:{mr['target_branch']}"
        by_key[key] = mr
        by_key.setdefault(mr["service_key"], mr)

    jobs_by_id = {db["jobs"][jid]["job_id"]: db["jobs"][jid] for jid in task.get("jobs", []) if jid in db["jobs"]}
    now = _now()
    for sid in task.get("sub_tasks", []):
        st = db["sub_tasks"].get(sid)
        if not st or st.get("section") != "build" or st.get("build_only"):
            continue
        job = jobs_by_id.get(st.get("job_id", ""))
        src = (job or {}).get("branch_from", "")
        tgt = (job or {}).get("branch_to", "")
        mr = by_key.get(f"{st['service_key']}:{src}:{tgt}") or by_key.get(st["service_key"])
        if not mr or not mr.get("iid"):
            continue
        params = dict(st.get("jenkins_params") or {})
        params["MergeID"] = f"!{mr['iid']}"
        st["jenkins_params"] = params
        st["merge_request"] = {"iid": mr["iid"], "web_url": mr.get("web_url", "")}
        st["updated_at"] = now


def _merge_task_merge_requests(db: dict[str, Any], task: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge all open MRs for a task. Called when Dev Lead approves."""
    report = task.get("validation_report") or {}
    mrs = list(task.get("merge_requests") or _extract_merge_requests_from_report(report))
    if not mrs:
        return []

    client = gitspace.get_client()
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    now = _now()

    for mr in mrs:
        iid = int(mr.get("iid") or 0)
        project = (mr.get("project") or "").strip()
        label = mr.get("label") or mr.get("service_key") or "service"
        if not iid or not project:
            continue
        if mr.get("state") == "merged":
            results.append({**mr, "merge_result": {"ok": True, "detail": f"MR !{iid} already merged."}})
            continue

        refreshed = client.get_merge_request(project, iid)
        if refreshed:
            if refreshed.get("has_conflicts") or refreshed.get("mergeable") is False:
                errors.append(f"{label}: MR !{iid} has conflicts — resolve in GitSpace first.")
                continue
            mr.update({
                "state": refreshed.get("state", mr.get("state")),
                "mergeable": refreshed.get("mergeable"),
                "has_conflicts": refreshed.get("has_conflicts"),
            })

        res = client.accept_merge_request(project, iid)
        if res.get("ok"):
            mr["state"] = "merged"
            mr["merged_at"] = now
            results.append({**mr, "merge_result": res})
        else:
            errors.append(f"{label}: {res.get('detail', 'merge failed')}")

    task["merge_requests"] = mrs
    if results:
        task["merge_results"] = results
        _sync_subtask_merge_ids(db, task, mrs)

    if errors:
        raise ApprovalError("Could not merge all GitSpace MRs: " + "; ".join(errors))
    return results


def _sync_yaml_deployments(
    db: dict[str, Any],
    task: dict[str, Any],
    report: dict[str, Any],
) -> None:
    """Attach validated YAML file list + Jenkins params to each yaml sub-task."""
    items_by_key: dict[str, list[dict[str, Any]]] = {}
    for g in report.get("sections") or []:
        if g.get("section") != "yaml":
            continue
        for it in g.get("items") or []:
            key = it.get("service_key", "")
            if key:
                items_by_key.setdefault(key, []).append(it)

    task_row = db["tasks"].get(task["task_id"], task)
    environment = task_row.get("environment", "INTEG")
    release_tag = (task_row.get("release_tag") or "").strip()
    now = _now()

    for sid in task.get("sub_tasks", []):
        st = db["sub_tasks"].get(sid)
        if not st or st.get("section") != "yaml":
            continue
        matches = items_by_key.get(st.get("service_key", ""), [])
        item = matches[0] if matches else {}
        files = list(item.get("yaml_files") or [])
        if not files:
            files = [{"path": "", "filename": "parameters.yml", "action": "Update"}]
        deployments: list[dict[str, Any]] = []
        release_branch = (st.get("release_branch") or "").strip()
        sub_type = st.get("sub_type") or "microservice"
        label = st.get("label") or ""
        for f in files:
            action = f.get("action") or jenkins_params.yaml_action_for_filename(f.get("filename", ""))
            params = jenkins_params.build_yml_automation_params(
                service_label=label,
                sub_type=sub_type,
                environment=environment,
                release_branch=release_branch,
                release_tag=release_tag,
                action=action,
            )
            deployments.append({
                "path": f.get("path", ""),
                "filename": f.get("filename", ""),
                "action": action,
                "jenkins_params": params,
            })
        st["yaml_deployments"] = deployments
        st["jenkins_job"] = jenkins_params.yml_automation_job_path()
        if deployments:
            st["jenkins_params"] = deployments[0]["jenkins_params"]
        st["updated_at"] = now


def _sync_db_jenkins_params(db: dict[str, Any], task: dict[str, Any]) -> None:
    """Attach DB-Script-Automation-Liquibase Jenkins params to each db sub-task."""
    task_row = db["tasks"].get(task["task_id"], task)
    environment = task_row.get("environment", "INTEG")
    release_tag = (task_row.get("release_tag") or "").strip()
    now = _now()
    for sid in task.get("sub_tasks", []):
        st = db["sub_tasks"].get(sid)
        if not st or st.get("section") != "db":
            continue
        params = jenkins_params.build_db_liquibase_params(
            service_label=st.get("label") or "",
            sub_type=st.get("sub_type") or "microservice",
            environment=environment,
            release_branch=(st.get("release_branch") or "").strip(),
            release_tag=release_tag,
            action=st.get("db_action") or "Update",
        )
        st["jenkins_params"] = params
        st["jenkins_job"] = jenkins_params.db_liquibase_job_path()
        st["updated_at"] = now


def _discover_phrases_json_files(st: dict[str, Any]) -> list[dict[str, Any]]:
    """List Phrases/*.json on the release branch with Add/Delete actions."""
    svc = catalog.get_service(st.get("service_key", ""))
    if not svc:
        return [{"path": "", "filename": "Phrases.json", "action": "Add"}]
    service = dict(svc)
    service["type"] = "portal"
    service["phrases_kind"] = st.get("sub_type") or service.get("phrases_kind") or "phrases"
    release = (st.get("release_branch") or "").strip()
    cfg = gitspace.load_config()
    client = gitspace.get_client()
    files = gitspace.resolve_section_files("phrases", service, release, cfg, client)
    if not files:
        return [{"path": "", "filename": "Phrases.json", "action": "Add"}]
    meta = [
        {
            "path": fp,
            "filename": fp.rsplit("/", 1)[-1],
            "action": jenkins_params.json_action_for_phrases_filename(fp.rsplit("/", 1)[-1]),
        }
        for fp in files
    ]
    return jenkins_params.sort_phrases_json_deployments(meta)


def _sync_phrases_jenkins_params(
    db: dict[str, Any],
    task: dict[str, Any],
    report: dict[str, Any] | None = None,
) -> None:
    """Attach Json_Automation_V2 Jenkins params to each Json & SchemaForms sub-task."""
    report = report or task.get("validation_report") or {}
    items_by_key: dict[str, list[dict[str, Any]]] = {}
    for g in report.get("sections") or []:
        if g.get("section") != "phrases":
            continue
        for it in g.get("items") or []:
            key = it.get("service_key", "")
            if key:
                items_by_key.setdefault(key, []).append(it)

    task_row = db["tasks"].get(task["task_id"], task)
    environment = task_row.get("environment", "INTEG")
    release_tag = (task_row.get("release_tag") or "").strip()
    now = _now()
    for sid in task.get("sub_tasks", []):
        st = db["sub_tasks"].get(sid)
        if not st or st.get("section") != "phrases":
            continue
        sub_type = st.get("sub_type") or "phrases"
        update_kind = jenkins_params.phrases_update_for_sub_type(sub_type)
        label = st.get("label") or ""
        release_branch = (st.get("release_branch") or "").strip()
        matches = items_by_key.get(st.get("service_key", ""), [])
        item = matches[0] if matches else {}
        deployments: list[dict[str, Any]] = []

        if update_kind == "Json":
            files = list(item.get("json_files") or [])
            if not files:
                files = _discover_phrases_json_files(st)
            for f in jenkins_params.sort_phrases_json_deployments(files):
                action = f.get("action") or "Add"
                params = jenkins_params.build_json_automation_params(
                    portal_label=label,
                    environment=environment,
                    phrases_branch=release_branch,
                    release_version=release_tag,
                    sub_type=sub_type,
                    update="Json",
                    action=action,
                )
                deployments.append({
                    "path": f.get("path", ""),
                    "filename": f.get("filename", ""),
                    "action": action,
                    "update": "Json",
                    "jenkins_params": params,
                })
        else:
            params = jenkins_params.build_json_automation_params(
                portal_label=label,
                environment=environment,
                phrases_branch=release_branch,
                release_version=release_tag,
                sub_type=sub_type,
                update=update_kind,
            )
            deployments.append({
                "path": "",
                "filename": "",
                "update": update_kind,
                "jenkins_params": params,
            })

        st["json_deployments"] = deployments
        st["jenkins_job"] = jenkins_params.json_automation_job_path()
        if deployments:
            st["jenkins_params"] = deployments[0]["jenkins_params"]
        st["updated_at"] = now


def _sync_jenkins_subtask_params(
    db: dict[str, Any],
    task: dict[str, Any],
    report: dict[str, Any] | None = None,
) -> None:
    """Refresh Jenkins params on yaml, db, and phrases sub-tasks after DevOps sets RELEASE_TAG."""
    report = report or task.get("validation_report") or {}
    _sync_yaml_deployments(db, task, report)
    _sync_db_jenkins_params(db, task)
    _sync_phrases_jenkins_params(db, task, report)


def apply_approval_decision(
    task_id: str,
    role: str,
    decision: str,
    by: str,
    comment: str | None = None,
    release_tag: str | None = None,
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

        if decision == "approved" and role == "dev_lead":
            _merge_task_merge_requests(db, task)

        if (
            decision == "approved"
            and role == "devops"
            and _task_needs_release_tag(db, task)
            and not (release_tag and release_tag.strip())
        ):
            raise ApprovalError("Release tag (RELEASE_TAG) is required for DevOps approval")

        now = _now()
        task["approvals"][role] = {"decision": decision, "comment": comment, "by": by, "at": now}
        task["updated_at"] = now

        if decision == "approved" and role == "devops" and release_tag:
            task["release_tag"] = release_tag.strip()
            task["release_tag_set_by"] = by
            task["release_tag_set_at"] = now
            if _task_needs_release_tag(db, task):
                _refresh_build_jenkins_params(db, task)
            _sync_jenkins_subtask_params(db, task, task.get("validation_report") or {})

        if decision == "rejected":
            task["status"] = "rejected"
            task["rejection_reason"] = comment
            _write(db)
            return _expand_task(db, task_id)

        next_stage = get_current_approval_stage(task)
        if next_stage is None:
            if _task_needs_release_tag(db, task):
                _refresh_build_jenkins_params(db, task)
                _sync_jenkins_subtask_params(db, task, task.get("validation_report") or {})
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
            # In mock mode the phase machine starts Phase 1 sub-tasks and the
            # frontend drives them via /tick. In langgraph mode the background
            # orchestrator_runner drives sub-tasks, so we skip the mock start.
            if _orchestrator_mode() != "langgraph":
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
    sub_task_id: str,
    steps: list[dict[str, Any]],
    status: str,
    detail: str | None = None,
    *,
    jenkins_build_number: int | None = None,
    jenkins_triggered: bool | None = None,
    extra_logs: list[str] | None = None,
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
        if jenkins_build_number is not None:
            st["jenkins_build_number"] = jenkins_build_number
            st["mock_build_number"] = jenkins_build_number
        if jenkins_triggered is not None:
            st["jenkins_triggered"] = jenkins_triggered
        if detail:
            st["logs"].append(f"[{now}] {detail}")
        for line in extra_logs or []:
            st["logs"].append(f"[{now}] {line}")
        _write(db)
        return st


def prepare_json_deployment_rerun(
    sub_task_id: str,
    deployment: dict[str, Any],
    deploy_index: int,
) -> None:
    """Re-open a Json & SchemaForms sub-task for the next serial Jenkins run."""
    with _LOCK:
        db = _read()
        st = db["sub_tasks"].get(sub_task_id)
        if not st or st.get("section") != "phrases":
            return
        now = _now()
        st["status"] = "running"
        st["updated_at"] = now
        if deployment.get("jenkins_params"):
            st["jenkins_params"] = deployment["jenkins_params"]
        st["jenkins_build_number"] = None
        st["jenkins_build_url"] = None
        st["jenkins_triggered"] = False
        st["console_log"] = None
        label = deployment.get("update") or jenkins_params.phrases_update_for_sub_type(st.get("sub_type", ""))
        action = deployment.get("action")
        detail = f"{label} {action}" if action else label
        st["logs"].append(
            f"[{now}] Json deploy {deploy_index + 1}: {detail} — {deployment.get('filename') or 'folder'}"
        )
        for step in st.get("steps", []):
            if step.get("step_id") in ("s2", "s3", "s4", "s5"):
                step["status"] = "queued"
                step["detail"] = None
                step["ts"] = None
        _write(db)


def prepare_yaml_deployment_rerun(
    sub_task_id: str,
    deployment: dict[str, Any],
    deploy_index: int,
) -> None:
    """Re-open a yaml sub-task for the next file in a serial multi-file deploy."""
    with _LOCK:
        db = _read()
        st = db["sub_tasks"].get(sub_task_id)
        if not st or st.get("section") != "yaml":
            return
        now = _now()
        st["status"] = "running"
        st["updated_at"] = now
        if deployment.get("jenkins_params"):
            st["jenkins_params"] = deployment["jenkins_params"]
        st["jenkins_build_number"] = None
        st["jenkins_build_url"] = None
        st["jenkins_triggered"] = False
        st["console_log"] = None
        st["logs"].append(
            f"[{now}] YAML deploy {deploy_index + 1}: "
            f"{deployment.get('action', 'Update')} — {deployment.get('filename', 'parameters.yml')}"
        )
        for step in st.get("steps", []):
            if step.get("step_id") in ("s3", "s4", "s5", "s6"):
                step["status"] = "queued"
                step["detail"] = None
                step["ts"] = None
        _write(db)


def record_agent_event(
    sub_task_id: str,
    *,
    status: str | None = None,
    step_index: int | None = None,
    step_status: str | None = None,
    step_detail: str | None = None,
    log_lines: list[str] | None = None,
    jenkins_build_number: int | None = None,
    jenkins_build_url: str | None = None,
    console_log: str | None = None,
    ai_report: dict[str, Any] | None = None,
    retry_count: int | None = None,
    failure_category: str | None = None,
) -> dict[str, Any] | None:
    """Persist one LangGraph orchestrator node's progress onto a sub-task.

    Used by orchestrator_graph nodes (via orchestrator_runner) so the portal UI
    reflects live agent state: current step, logs, retries, Jenkins build number,
    and the AI failure/success report.
    """
    with _LOCK:
        db = _read()
        st = db["sub_tasks"].get(sub_task_id)
        if not st:
            return None
        now = _now()
        if step_index is not None and 0 <= step_index < len(st.get("steps", [])):
            step = st["steps"][step_index]
            if step_status is not None:
                step["status"] = step_status
            step["detail"] = step_detail
            step["ts"] = now
        if status is not None:
            st["status"] = status
        if jenkins_build_number is not None:
            st["jenkins_build_number"] = jenkins_build_number
            st["mock_build_number"] = jenkins_build_number
            st["jenkins_triggered"] = True
            # Keep the wait-step label in sync with the real Jenkins build number.
            steps = st.get("steps", [])
            if len(steps) > 3:
                steps[3]["label"] = f"Wait for Jenkins build #{jenkins_build_number} to complete"
        if jenkins_build_url is not None:
            st["jenkins_build_url"] = jenkins_build_url
        if console_log is not None:
            st["console_log"] = console_log
        if ai_report is not None:
            st["ai_report"] = ai_report
        if retry_count is not None:
            st["retry_count"] = retry_count
        if failure_category is not None:
            st["failure_category"] = failure_category

        # ── Per-build attempt history (drives the UI's build-log dropdown) ──
        # Each Jenkins trigger (initial, auto-retry, or manual retry) creates a
        # new entry so DevOps can compare a previous build's console/report with
        # the current one. History is append-only and never overwritten.
        attempts = st.setdefault("build_attempts", [])
        if jenkins_build_number is not None:
            latest = attempts[-1] if attempts else None
            if latest is None or latest.get("build_number") != jenkins_build_number:
                attempts.append({
                    "attempt": len(attempts) + 1,
                    "build_number": jenkins_build_number,
                    "build_url": jenkins_build_url,
                    "status": "running",
                    "console_log": None,
                    "ai_report": None,
                    "category": None,
                    "started_at": now,
                    "finished_at": None,
                })
            elif jenkins_build_url is not None:
                latest["build_url"] = jenkins_build_url
        current_attempt = attempts[-1] if attempts else None
        if current_attempt is not None:
            if console_log is not None:
                current_attempt["console_log"] = console_log
            if ai_report is not None:
                current_attempt["ai_report"] = ai_report
                if ai_report.get("category"):
                    current_attempt["category"] = ai_report["category"]
            if failure_category is not None:
                current_attempt["category"] = failure_category
            if status in ("done", "failed"):
                current_attempt["status"] = status
                current_attempt["finished_at"] = now

        for line in log_lines or []:
            st["logs"].append(f"[{now}] {line}")
        st["updated_at"] = now
        _write(db)
        return st


def reset_sub_task_for_retry(sub_task_id: str) -> dict[str, Any] | None:
    """Re-queue a failed sub-task for a fresh manual build run.

    Keeps ``build_attempts`` history intact (so the previous console/report stay
    available in the dropdown) but clears the *current* console/report and resets
    every step to queued so the UI shows a clean new attempt.
    """
    with _LOCK:
        db = _read()
        st = db["sub_tasks"].get(sub_task_id)
        if not st:
            return None
        now = _now()
        for step in st.get("steps", []):
            step["status"] = "queued"
            step["detail"] = None
            step["ts"] = None
        st["status"] = "queued"
        st["console_log"] = None
        st["ai_report"] = None
        st["failure_category"] = None
        st["logs"].append(f"[{now}] ── Manual retry requested — starting a new build ──")
        st["updated_at"] = now
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
    build_only: bool = False,
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

    if section == "build" and sub_type == "microservice":
        jenkins_job = jenkins_params.microservices_job_path()
        jenkins_params_dict = jenkins_params.build_titan_microservices_params(service_label=label)
    elif section == "build" and sub_type == "portal":
        jenkins_job = jenkins_params.portals_job_path()
        jenkins_params_dict = jenkins_params.build_titan_portals_params(portal_label=label)
    elif section == "build":
        jenkins_params_dict = {"Service": label, "MergeID": f"!{mr_id}", "ReleaseBranch": release_branch or ""}
    elif section == "yaml":
        jenkins_job = jenkins_params.yml_automation_job_path()
        jenkins_params_dict = jenkins_params.build_yml_automation_params(
            service_label=label,
            sub_type=sub_type or "microservice",
            environment="INTEG",
            release_branch=release_branch or "",
            release_tag="",
            action="Update",
        )
    elif section == "db":
        jenkins_job = jenkins_params.db_liquibase_job_path()
        jenkins_params_dict = jenkins_params.build_db_liquibase_params(
            service_label=label,
            sub_type=sub_type or "microservice",
            environment="INTEG",
            release_branch=release_branch or "",
            release_tag="",
            action="Update",
        )
    elif section == "phrases":
        jenkins_job = jenkins_params.json_automation_job_path()
        jenkins_params_dict = jenkins_params.build_json_automation_params(
            portal_label=label,
            environment="INTEG",
            phrases_branch=release_branch or "",
            release_version="",
            sub_type=sub_type or "phrases",
        )
    else:
        jenkins_params_dict = {}

    ctx["jenkins_job"] = jenkins_job or "jenkins"

    db["sub_tasks"][sub_task_id] = {
        "sub_task_id": sub_task_id,
        "job_id": job_id,
        "task_id": task_id,
        "section": section,
        "sub_type": sub_type,
        "service_key": service_key,
        "label": label,
        "release_branch": release_branch or None,
        "build_only": bool(build_only),
        "order": 0,  # assigned by _build_orchestrator_plan
        "status": "queued",
        "agent": _SUB_TASK_AGENT.get(section, "orchestrator"),
        "steps": _make_sub_task_steps(section, sub_type, ctx),
        "logs": [],
        "console_log": None,
        "build_attempts": [],
        "jenkins_job": jenkins_job,
        "jenkins_params": jenkins_params_dict,
        "jenkins_build_number": None,
        "jenkins_build_url": None,
        "jenkins_triggered": False,
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
        raise ValidationError("At least one section (YAML / DB / Json & SchemaForms / Build) is required")

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
                "build_only": bool(sec.get("build_only")) if section == "build" else False,
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
                    build_only=bool(sec.get("build_only")) if section == "build" else False,
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
            "release_tag": None,
            "release_tag_set_by": None,
            "release_tag_set_at": None,
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
    period: str | None = None,
) -> list[dict[str, Any]]:
    db = _read()
    tasks = list(db["tasks"].values())
    if period:
        tasks = [t for t in tasks if _task_within_period(t, period)]
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
        mrs = _extract_merge_requests_from_report(report)
        if mrs:
            task["merge_requests"] = mrs
            _sync_subtask_merge_ids(db, task, mrs)
        _sync_jenkins_subtask_params(db, task, report)
        task["updated_at"] = _now()
        _write(db)
        return _expand_task(db, task_id)


def recover_stuck_validation_status() -> int:
    """After a server reload, in-flight validation threads die but status stays `running`.

    If a report already exists, mark ready so the UI can show it. Returns count fixed.
    """
    with _LOCK:
        db = _read()
        fixed = 0
        for task in db["tasks"].values():
            if task.get("validation_status") == "running" and task.get("validation_report"):
                task["validation_status"] = "ready"
                task["updated_at"] = _now()
                fixed += 1
        if fixed:
            _write(db)
        return fixed


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
    tasks = [t for t in db["tasks"].values() if _task_within_period(t, period)]
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


def get_recent_activity(limit: int = 6, period: str | None = None) -> list[dict[str, Any]]:
    tasks = list_tasks(limit=limit, period=period)
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
