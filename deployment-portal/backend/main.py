"""
main.py — Deployment Portal API (v2)

Run:
    pip install -r requirements.txt
    uvicorn main:app --reload --host 0.0.0.0 --port 9002
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel

import catalog
import db
from auth import authenticate_user, create_access_token, get_current_user

app = FastAPI(title="Deployment Portal API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SectionKey = Literal["build", "yaml", "db"]
SubType = Literal["microservice", "portal", "utility"]


class LinkInput(BaseModel):
    sub_type: SubType
    url: str
    label: str = ""


class SectionInput(BaseModel):
    section: SectionKey
    links: list[LinkInput]


class CreateTaskRequest(BaseModel):
    environment: str
    jira_id: str
    description: str = ""
    branch_from: str = ""
    branch_to: str = ""
    approver_key: str
    sections: list[SectionInput]


class UpdateJobStatusRequest(BaseModel):
    status: Literal["queued", "running", "done", "failed"]
    log_line: str | None = None


class ApproveTaskRequest(BaseModel):
    role: Literal["approver", "devops"]


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


@app.get("/catalog/environments")
def get_environments(current_user: dict = Depends(get_current_user)) -> list[dict[str, Any]]:
    return catalog.load_environments()


@app.get("/catalog/approvers")
def get_approvers(current_user: dict = Depends(get_current_user)) -> list[dict[str, Any]]:
    return catalog.load_approvers()


@app.post("/tasks")
def create_task(
    payload: CreateTaskRequest,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    sections_payload = [
        {"section": s.section, "links": [l.model_dump() for l in s.links]}
        for s in payload.sections
    ]

    try:
        task = db.create_task(
            environment=payload.environment,
            jira_id=payload.jira_id,
            description=payload.description,
            branch_from=payload.branch_from,
            branch_to=payload.branch_to,
            approver_key=payload.approver_key,
            requested_by=current_user["email"],
            sections_payload=sections_payload,
        )
    except db.ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return task


@app.get("/tasks")
def list_tasks(
    status: str | None = None,
    requested_by: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    return db.list_tasks(status=status, requested_by=requested_by)


@app.get("/tasks/{task_id}")
def get_task(task_id: str, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    approver = catalog.get_approver(task.get("approver_key", ""))
    if approver:
        task["approver_name"] = approver["name"]
    return task


@app.post("/tasks/{task_id}/approve")
def approve_task(
    task_id: str,
    payload: ApproveTaskRequest,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        task = db.approve_task(task_id, payload.role)
    except db.ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    approver = catalog.get_approver(task.get("approver_key", ""))
    if approver:
        task["approver_name"] = approver["name"]
    return task


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


@app.post("/demo/seed")
def seed_demo_data(current_user: dict = Depends(get_current_user)) -> dict[str, str]:
    db.reset_db()
    seed_user = current_user["email"]

    db.create_task(
        environment="INTEG",
        jira_id="TRB-16996",
        description="Account service update + billing config + DB migration",
        branch_from="develop",
        branch_to="release/2026-06",
        approver_key="a-sharma",
        requested_by=seed_user,
        sections_payload=[
            {
                "section": "yaml",
                "links": [
                    {"sub_type": "microservice", "url": "https://gitspace/.../!2816", "label": "parameters.yml"},
                ],
            },
            {
                "section": "db",
                "links": [
                    {"sub_type": "microservice", "url": "https://gitspace/.../changelog.sql", "label": "billing migration"},
                ],
            },
            {
                "section": "build",
                "links": [
                    {"sub_type": "microservice", "url": "https://gitspace/.../!2814", "label": "AccountService"},
                    {"sub_type": "portal", "url": "https://gitspace/.../!2815", "label": "Admin Portal"},
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
        requested_by=seed_user,
        sections_payload=[
            {
                "section": "build",
                "links": [
                    {"sub_type": "portal", "url": "https://gitspace/.../!2820", "label": "Admin Portal"},
                ],
            },
        ],
    )
    db.approve_task(t2["task_id"], "approver")
    db.approve_task(t2["task_id"], "devops")
    t2 = db.get_task(t2["task_id"])
    db.update_job_status(t2["jobs"][0]["job_id"], "done", "Portal deployed successfully")

    t3 = db.create_task(
        environment="UAT",
        jira_id="TRB-18055",
        description="Apollo SQL migration — missing environment field",
        branch_from="release/2026-05",
        branch_to="main",
        approver_key="d-kumar",
        requested_by=seed_user,
        sections_payload=[
            {
                "section": "db",
                "links": [
                    {"sub_type": "microservice", "url": "https://gitspace/.../apollo.sql", "label": "apollo migration"},
                ],
            },
        ],
    )
    db.approve_task(t3["task_id"], "approver")
    db.approve_task(t3["task_id"], "devops")
    t3 = db.get_task(t3["task_id"])
    db.update_job_status(t3["jobs"][0]["job_id"], "failed", "Blocked: environment field required before run")

    return {"status": "seeded"}


@app.post("/demo/reset")
def reset_demo_data(current_user: dict = Depends(get_current_user)) -> dict[str, str]:
    db.reset_db()
    return {"status": "reset"}
