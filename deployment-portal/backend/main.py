"""
main.py — Deployment Portal API (v3)

Run:
    pip install -r requirements.txt
    uvicorn main:app --reload --host 0.0.0.0 --port 9002
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import catalog
import db
import mock_gitlab
import notifications

app = FastAPI(title="Deployment Portal API", version="0.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SectionKey = Literal["build", "yaml", "db", "phrases"]
SubType = Literal["microservice", "portal", "utility"]


class LinkInput(BaseModel):
    sub_type: SubType
    service_key: str
    label: str = ""


class SectionInput(BaseModel):
    section: SectionKey
    release_branch: str = ""
    links: list[LinkInput]


class CreateTaskRequest(BaseModel):
    environment: str
    jira_id: str
    description: str = ""
    branch_from: str = ""
    branch_to: str = ""
    approver_key: str
    sections: list[SectionInput]


class ValidateRequest(BaseModel):
    environment: str
    jira_id: str
    sections: list[SectionInput]


class UpdateJobStatusRequest(BaseModel):
    status: Literal["queued", "running", "done", "failed"]
    log_line: str | None = None


class ApprovalRequest(BaseModel):
    role: Literal["dev_lead", "qa", "devops"]
    decision: Literal["approved", "rejected"]
    by: str = "demo-approver"
    comment: str | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ──────────────────────────────────────────────────────────────────────────
# Catalog
# ──────────────────────────────────────────────────────────────────────────

@app.get("/catalog/environments")
def get_environments() -> list[dict[str, Any]]:
    return catalog.load_environments()


@app.get("/catalog/approvers")
def get_approvers() -> list[dict[str, Any]]:
    return catalog.load_approvers()


@app.get("/catalog/services")
def get_services(
    section: str | None = None,
    type: Literal["microservice", "portal", "utility"] | None = None,
) -> list[dict[str, Any]]:
    return catalog.load_services(section=section, type=type)


@app.get("/catalog/branches")
def get_branches(service_key: str, query: str = "") -> list[str]:
    svc = catalog.get_service(service_key)
    if not svc:
        raise HTTPException(status_code=404, detail="Unknown service")
    return mock_gitlab.list_branches(svc["gitlab_project_path"], query)


@app.get("/catalog/code-freeze")
def get_code_freeze() -> dict[str, Any]:
    return catalog.load_code_freeze()


@app.put("/catalog/code-freeze")
def set_code_freeze(enabled: bool, updated_by: str = "demo-devops") -> dict[str, Any]:
    # TODO when real auth lands: require DevOps role before allowing this.
    return catalog.save_code_freeze(enabled, updated_by)


# ──────────────────────────────────────────────────────────────────────────
# Tasks
# ──────────────────────────────────────────────────────────────────────────

@app.post("/tasks/validate")
def validate_request(payload: ValidateRequest) -> dict[str, Any]:
    """Validate-before-submit. Never raises — returns an error checklist."""
    errors: list[str] = []

    if not payload.environment:
        errors.append("Environment to promote is required")
    if not payload.jira_id.strip():
        errors.append("Jira ID is required")
    if not payload.sections:
        errors.append("At least one section (YAML / DB / Phrases / Build) must be filled")

    for sec in payload.sections:
        try:
            db.validate_section_links(sec.section, [l.model_dump() for l in sec.links])
        except db.ValidationError as e:
            errors.append(str(e))
        if sec.section in db.SECTIONS_NEEDING_RELEASE_BRANCH and not sec.release_branch.strip():
            errors.append(f"Release branch is required for the {sec.section} section")

    return {"valid": len(errors) == 0, "errors": errors}


@app.post("/tasks")
def create_task(payload: CreateTaskRequest) -> dict[str, Any]:
    sections_payload = [s.model_dump() for s in payload.sections]
    code_freeze = catalog.load_code_freeze()["enabled"]

    try:
        task = db.create_task(
            environment=payload.environment,
            jira_id=payload.jira_id,
            description=payload.description,
            branch_from=payload.branch_from,
            branch_to=payload.branch_to,
            approver_key=payload.approver_key,
            requested_by="demo-user",
            sections_payload=sections_payload,
            code_freeze_enabled=code_freeze,
        )
    except db.ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    notifications.notify_submitted(task)
    return _decorate(task)


@app.get("/tasks")
def list_tasks(
    status: str | None = None,
    requested_by: str | None = None,
) -> list[dict[str, Any]]:
    return [_decorate(t) for t in db.list_tasks(status=status, requested_by=requested_by)]


@app.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return _decorate(task)


@app.post("/tasks/{task_id}/approve")
def approve_task(task_id: str, payload: ApprovalRequest) -> dict[str, Any]:
    try:
        task = db.apply_approval_decision(
            task_id, payload.role, payload.decision, payload.by, payload.comment
        )
    except db.ApprovalError as e:
        msg = str(e)
        status_code = 422 if "comment is required" in msg else 409
        raise HTTPException(status_code=status_code, detail=msg)

    if payload.decision == "rejected":
        notifications.notify_rejected(task, payload.role, payload.comment or "")
    else:
        notifications.notify_approved_stage(task, payload.role, task.get("current_stage"))

    return _decorate(task)


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


@app.patch("/jobs/{job_id}/status")
def update_job_status(job_id: str, payload: UpdateJobStatusRequest) -> dict[str, Any]:
    job = db.update_job_status(job_id, payload.status, payload.log_line)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


@app.get("/stats")
def get_stats(period: Literal["daily", "weekly", "monthly"] = "weekly") -> dict[str, Any]:
    return db.get_stats(period)


@app.get("/activity")
def get_activity(limit: int = 6) -> list[dict[str, Any]]:
    return db.get_recent_activity(limit=limit)


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def _decorate(task: dict[str, Any]) -> dict[str, Any]:
    """Attach human-friendly names resolved from the catalog."""
    approver = catalog.get_approver(task.get("approver_key", ""))
    if approver:
        task["approver_name"] = approver["name"]
    services = {s["key"]: s["label"] for s in catalog.load_services()}
    for job in task.get("jobs", []):
        for link in job.get("links", []):
            if not link.get("label"):
                link["label"] = services.get(link.get("service_key", ""), link.get("service_key", ""))
    return task


# ──────────────────────────────────────────────────────────────────────────
# Demo helpers
# ──────────────────────────────────────────────────────────────────────────

@app.post("/demo/seed")
def seed_demo_data() -> dict[str, str]:
    db.reset_db()

    db.create_task(
        environment="INTEG",
        jira_id="TRB-16996",
        description="Account service update + billing config + DB migration",
        branch_from="develop",
        branch_to="release/2026-06",
        approver_key="a-sharma",
        requested_by="demo-user",
        code_freeze_enabled=False,
        sections_payload=[
            {
                "section": "yaml",
                "release_branch": "release/2026-07",
                "links": [
                    {"sub_type": "microservice", "service_key": "yaml:microservice:account", "label": "Account"},
                ],
            },
            {
                "section": "db",
                "release_branch": "release/2026-07",
                "links": [
                    {"sub_type": "microservice", "service_key": "db:microservice:payment", "label": "Payment"},
                ],
            },
            {
                "section": "build",
                "release_branch": "",
                "links": [
                    {"sub_type": "microservice", "service_key": "build:microservice:account", "label": "Account"},
                    {"sub_type": "portal", "service_key": "build:portal:admin", "label": "Admin"},
                ],
            },
        ],
    )

    t2 = db.create_task(
        environment="INTEG",
        jira_id="TRB-17812",
        description="Admin portal refresh",
        branch_from="develop",
        branch_to="main",
        approver_key="r-patel",
        requested_by="demo-user",
        code_freeze_enabled=False,
        sections_payload=[
            {
                "section": "build",
                "release_branch": "",
                "links": [
                    {"sub_type": "portal", "service_key": "build:portal:ecommerce", "label": "Ecommerce"},
                ],
            },
        ],
    )
    db.apply_approval_decision(t2["task_id"], "dev_lead", "approved", "Dev Lead")
    db.apply_approval_decision(t2["task_id"], "devops", "approved", "DevOps")
    t2 = db.get_task(t2["task_id"])
    db.update_job_status(t2["jobs"][0]["job_id"], "done", "Portal deployed successfully")

    t3 = db.create_task(
        environment="UAT",
        jira_id="TRB-18055",
        description="Apollo SQL migration — rejected for missing environment field",
        branch_from="release/2026-05",
        branch_to="main",
        approver_key="d-kumar",
        requested_by="demo-user",
        code_freeze_enabled=False,
        sections_payload=[
            {
                "section": "db",
                "release_branch": "release/2026-06",
                "links": [
                    {"sub_type": "microservice", "service_key": "db:microservice:order", "label": "Order"},
                ],
            },
        ],
    )
    db.apply_approval_decision(
        t3["task_id"], "dev_lead", "rejected", "Dev Lead",
        comment="Environment field is required before this can run.",
    )

    return {"status": "seeded"}


@app.post("/demo/reset")
def reset_demo_data() -> dict[str, str]:
    db.reset_db()
    return {"status": "reset"}
