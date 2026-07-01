"""
main.py — Deployment Portal API (v3)

Run:
    pip install -r requirements.txt
    uvicorn main:app --reload --host 0.0.0.0 --port 9002
"""

from __future__ import annotations

import threading
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field

import catalog
import db
import mock_gitlab
import notifications
import validation
from auth import (
    authenticate_user,
    create_access_token,
    get_current_user,
    role_to_stage,
)

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
    # Build groups carry their own source → destination branch pair.
    branch_from: str = ""
    branch_to: str = ""
    links: list[LinkInput]


class BranchPair(BaseModel):
    from_branch: str = Field("", alias="from")
    to: str = ""

    model_config = {"populate_by_name": True}


class CreateTaskRequest(BaseModel):
    environment: str
    jira_id: str
    description: str = ""
    branch_from: str = ""
    branch_to: str = ""
    branches: list[BranchPair] = []
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


# ──────────────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────────────

@app.post("/auth/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends()) -> dict[str, Any]:
    # OAuth2 spec names the field "username"; we accept an email address here.
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    email = user["email"]
    token = create_access_token({"sub": email})
    return {
        "access_token": token,
        "token_type": "bearer",
        "email": email,
        "display_name": user.get("display_name", email),
        "role": user.get("role", "developer"),
        "approval_stage": role_to_stage(user.get("role")),
    }


@app.post("/auth/logout")
async def logout() -> dict[str, str]:
    return {"message": "Logged out"}


@app.get("/auth/me")
async def me(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return current_user


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ──────────────────────────────────────────────────────────────────────────
# Catalog
# ──────────────────────────────────────────────────────────────────────────

@app.get("/catalog/environments")
def get_environments(current_user: dict = Depends(get_current_user)) -> list[dict[str, Any]]:
    return catalog.load_environments()


@app.get("/catalog/approvers")
def get_approvers(current_user: dict = Depends(get_current_user)) -> list[dict[str, Any]]:
    return catalog.load_approvers()


@app.get("/catalog/services")
def get_services(
    section: str | None = None,
    type: Literal["microservice", "portal", "utility"] | None = None,
    current_user: dict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    return catalog.load_services(section=section, type=type)


@app.get("/catalog/branches")
def get_branches(
    service_key: str,
    query: str = "",
    current_user: dict = Depends(get_current_user),
) -> list[str]:
    svc = catalog.get_service(service_key)
    if not svc:
        raise HTTPException(status_code=404, detail="Unknown service")
    return mock_gitlab.list_branches(svc["gitlab_project_path"], query)


@app.get("/catalog/code-freeze")
def get_code_freeze(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return catalog.load_code_freeze()


@app.put("/catalog/code-freeze")
def set_code_freeze(
    enabled: bool,
    updated_by: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    # Only DevOps may toggle the code freeze.
    if current_user.get("approval_stage") != "devops":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only DevOps can change the code freeze",
        )
    return catalog.save_code_freeze(enabled, updated_by or current_user["display_name"])


# ──────────────────────────────────────────────────────────────────────────
# Tasks
# ──────────────────────────────────────────────────────────────────────────

@app.post("/tasks/validate")
def validate_request(
    payload: ValidateRequest,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
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
def create_task(
    payload: CreateTaskRequest,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    sections_payload = [s.model_dump() for s in payload.sections]
    code_freeze = catalog.load_code_freeze()["enabled"]

    # Each Build group carries its own source → destination branch pair. Collect
    # them for the task-level summary; keep the first as branch_from/branch_to.
    branches = []
    for sec in sections_payload:
        if sec["section"] == "build":
            bf = (sec.get("branch_from") or "").strip()
            bt = (sec.get("branch_to") or "").strip()
            if bf or bt:
                branches.append({"from": bf, "to": bt})
    if not branches and payload.branches:
        branches = [
            {"from": b.from_branch.strip(), "to": b.to.strip()}
            for b in payload.branches
            if b.from_branch.strip() or b.to.strip()
        ]
    branch_from = branches[0]["from"] if branches else payload.branch_from
    branch_to = branches[0]["to"] if branches else payload.branch_to

    try:
        task = db.create_task(
            environment=payload.environment,
            jira_id=payload.jira_id,
            description=payload.description,
            branch_from=branch_from,
            branch_to=branch_to,
            branches=branches,
            approver_key=payload.approver_key,
            requested_by=current_user["display_name"],
            sections_payload=sections_payload,
            code_freeze_enabled=code_freeze,
        )
    except db.ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    notifications.notify_submitted(task)
    _kick_off_validation(task["task_id"], payload.environment, payload.jira_id, sections_payload)
    return _decorate(task)


@app.get("/tasks")
def list_tasks(
    status: str | None = None,
    requested_by: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    return [_decorate(t) for t in db.list_tasks(status=status, requested_by=requested_by)]


@app.get("/tasks/{task_id}")
def get_task(task_id: str, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return _decorate(task)


@app.post("/tasks/{task_id}/approve")
def approve_task(
    task_id: str,
    payload: ApprovalRequest,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    # Role gate: a user may only act on the stage that matches their role.
    user_stage = current_user.get("approval_stage")
    if not user_stage:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your role is not allowed to approve requests",
        )
    if payload.role != user_stage:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You can only act on the {user_stage} approval stage",
        )

    try:
        task = db.apply_approval_decision(
            task_id, payload.role, payload.decision, current_user["display_name"], payload.comment
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


def _sections_from_task(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Reconstruct the validation sections payload from a stored task's jobs."""
    sections = []
    for job in task.get("jobs", []):
        sections.append({
            "section": job.get("section"),
            "release_branch": job.get("release_branch", ""),
            "branch_from": job.get("branch_from", ""),
            "branch_to": job.get("branch_to", ""),
            "links": job.get("links", []),
        })
    return sections


@app.post("/tasks/{task_id}/revalidate")
def revalidate_task(
    task_id: str,
    use_ai: bool = True,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    _kick_off_validation(task_id, task["environment"], task["jira_id"], _sections_from_task(task))
    return {"status": "validating", "task_id": task_id}


@app.post("/validate/preview")
def validate_preview(
    payload: ValidateRequest,
    use_ai: bool = True,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Synchronous deep validation that does NOT need a saved task — used by the
    request form's "deep validate" and by scripts/run_validation.py."""
    sections = [s.model_dump() for s in payload.sections]
    return validation.run_validation(payload.environment, payload.jira_id, sections, use_ai=use_ai)


@app.post("/webhooks/gitspace")
async def gitspace_webhook(request: Request) -> dict[str, Any]:
    """Stub for the future GitSpace (GitLab) webhook integration.

    No auth on purpose (webhooks authenticate via a shared secret header, to be
    added with the real integration). For now it just acknowledges the event so
    the endpoint and contract exist; merge/MR state will be reconciled onto the
    relevant task here once GitSpace is live.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    event = request.headers.get("X-Gitlab-Event", body.get("object_kind", "unknown"))
    return {"received": True, "event": event}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


@app.patch("/jobs/{job_id}/status")
def update_job_status(
    job_id: str,
    payload: UpdateJobStatusRequest,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    job = db.update_job_status(job_id, payload.status, payload.log_line)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


@app.get("/stats")
def get_stats(
    period: Literal["daily", "weekly", "monthly"] = "weekly",
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    return db.get_stats(period)


@app.get("/activity")
def get_activity(
    limit: int = 6,
    current_user: dict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    return db.get_recent_activity(limit=limit)


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def _run_validation_bg(task_id: str, environment: str, jira_id: str, sections_payload: list[dict[str, Any]]) -> None:
    db.set_validation_status(task_id, "running")
    try:
        report = validation.run_validation(environment, jira_id, sections_payload, use_ai=True)
        db.set_validation_report(task_id, report)
    except Exception as e:  # never let a validation crash strand the task
        db.set_validation_report(task_id, {
            "generated_at": validation._now(),
            "environment": environment,
            "jira_id": jira_id,
            "overall_status": "failed",
            "stats": {"checked": 0, "passed": 0, "warnings": 0, "failed": 0},
            "sections": [],
            "steps": [f"Validation engine error: {e}"],
            "ai_used": False,
            "ai_model": "",
            "summary_markdown": f"## Verdict — BLOCKED\nValidation could not run: `{e}`",
        })


def _kick_off_validation(task_id: str, environment: str, jira_id: str, sections_payload: list[dict[str, Any]]) -> None:
    threading.Thread(
        target=_run_validation_bg,
        args=(task_id, environment, jira_id, sections_payload),
        daemon=True,
    ).start()


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
def seed_demo_data(current_user: dict = Depends(get_current_user)) -> dict[str, str]:
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

    for t in db.list_tasks():
        _kick_off_validation(t["task_id"], t["environment"], t["jira_id"], _sections_from_task(t))

    return {"status": "seeded"}


@app.post("/demo/reset")
def reset_demo_data(current_user: dict = Depends(get_current_user)) -> dict[str, str]:
    db.reset_db()
    return {"status": "reset"}
