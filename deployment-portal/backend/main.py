"""
main.py — Deployment Portal API (MVP)

Run:
    pip install fastapi uvicorn pydantic
    uvicorn main:app --reload --host 0.0.0.0 --port 9002

This mirrors the structure of the existing email-intake pipeline
(deployment_platform/*.py) but accepts structured form input instead
of free-text email. Swap db.py for a real database later — every
other file stays the same.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import db

app = FastAPI(title="Deployment Portal API", version="0.1.0")

# Allow the React dev server (localhost:3000 / :5173) to call this API directly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten this before real deployment
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────────────────────
# Request / response models
# ──────────────────────────────────────────────────────────────────────────

JobType = Literal["microservice", "yaml", "db", "portal", "script"]


class JobInput(BaseModel):
    job_type: JobType
    fields: dict[str, Any] = Field(default_factory=dict)


class CreateTaskRequest(BaseModel):
    jira_key: str
    environment: Literal["INTEG", "UAT", "PROD"] = "INTEG"
    priority: Literal["Normal", "High", "Urgent"] = "Normal"
    description: str = ""
    requested_by: str = "unknown"
    jobs: list[JobInput]


class UpdateJobStatusRequest(BaseModel):
    status: Literal["queued", "running", "done", "failed"]
    log_line: str | None = None


# ──────────────────────────────────────────────────────────────────────────
# Health
# ──────────────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ──────────────────────────────────────────────────────────────────────────
# Tasks
# ──────────────────────────────────────────────────────────────────────────

@app.post("/tasks")
def create_task(payload: CreateTaskRequest) -> dict[str, Any]:
    if not payload.jobs:
        raise HTTPException(status_code=400, detail="At least one job must be selected.")

    jobs_payload = [{"job_type": j.job_type, "fields": j.fields} for j in payload.jobs]

    task = db.create_task(
        jira_key=payload.jira_key,
        environment=payload.environment,
        priority=payload.priority,
        description=payload.description,
        requested_by=payload.requested_by,
        jobs_payload=jobs_payload,
    )
    return task


@app.get("/tasks")
def list_tasks(
    status: str | None = None,
    requested_by: str | None = None,
    period: str | None = None,
) -> list[dict[str, Any]]:
    return db.list_tasks(status=status, requested_by=requested_by, period=period)


@app.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return task


# ──────────────────────────────────────────────────────────────────────────
# Jobs
# ──────────────────────────────────────────────────────────────────────────

@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


@app.patch("/jobs/{job_id}/status")
def update_job_status(job_id: str, payload: UpdateJobStatusRequest) -> dict[str, Any]:
    """
    Manually advance a job's status — stands in for what would normally be
    a Jenkins webhook callback ("build finished, status=SUCCESS/FAILURE").
    Useful for the demo: lets you simulate the orchestrator progressing
    without actually wiring real Jenkins yet.
    """
    job = db.update_job_status(job_id, payload.status, payload.log_line)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


@app.post("/jobs/{job_id}/steps/{step_index}")
def advance_job_step(job_id: str, step_index: int, status: Literal["queued", "running", "done", "failed"]) -> dict[str, Any]:
    job = db.append_job_step(job_id, step_index, status)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} or step {step_index} not found")
    return job


# ──────────────────────────────────────────────────────────────────────────
# History / stats
# ──────────────────────────────────────────────────────────────────────────

@app.get("/stats")
def get_stats(period: Literal["daily", "weekly", "monthly"] = "weekly") -> dict[str, Any]:
    return db.get_stats(period)


# ──────────────────────────────────────────────────────────────────────────
# Demo helpers
# ──────────────────────────────────────────────────────────────────────────

@app.post("/demo/seed")
def seed_demo_data() -> dict[str, str]:
    """Wipes the DB and creates a few sample tasks — handy before a client demo."""
    db.reset_db()

    db.create_task(
        jira_key="TRB-16996",
        environment="INTEG",
        priority="High",
        description="Account service update + billing config + DB migration",
        requested_by="A. Sharma",
        jobs_payload=[
            {"job_type": "yaml", "fields": {"config_files": "parameters.yml, billing-rules.yml"}},
            {"job_type": "db", "fields": {"script_url": "gitspace/.../changelog.sql"}},
            {"job_type": "microservice", "fields": {
                "service": "AccountService", "merge_url": "gitspace/.../!2814", "branch": "feature/billing-rule"
            }},
        ],
    )

    t2 = db.create_task(
        jira_key="TRB-17812",
        environment="INTEG",
        priority="Normal",
        description="Admin portal refresh",
        requested_by="R. Patel",
        jobs_payload=[{"job_type": "portal", "fields": {"portal_name": "Admin Portal"}}],
    )
    # Mark this one fully done for demo variety
    db.update_job_status(t2["jobs"][0]["job_id"], "done", "Portal deployed successfully")

    t3 = db.create_task(
        jira_key="TRB-18055",
        environment="INTEG",
        priority="Normal",
        description="Apollo SQL migration — missing environment field",
        requested_by="D. Kumar",
        jobs_payload=[{"job_type": "db", "fields": {"script_url": "gitspace/.../apollo.sql"}}],
    )
    db.update_job_status(t3["jobs"][0]["job_id"], "failed", "Blocked: environment field required before run")

    return {"status": "seeded"}


@app.post("/demo/reset")
def reset_demo_data() -> dict[str, str]:
    db.reset_db()
    return {"status": "reset"}
