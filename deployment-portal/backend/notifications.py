"""
notifications.py — Mocked email notifications.

Logs to backend/notifications.log instead of actually sending email.
Every function signature here is what the real SMTP-backed version
will look like — swap the internals of _send(), nothing else changes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_PATH = Path(__file__).parent / "notifications.log"


def _send(to: str, subject: str, body: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry = f"[{ts}] TO: {to}\nSUBJECT: {subject}\n{body}\n{'-' * 60}\n"
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(entry)
    print(f"[MOCK EMAIL] to={to} subject={subject}")


def notify_submitted(task: dict[str, Any]) -> None:
    _send(
        to="dev-lead@company.com",
        subject=f"Approval needed: {task['jira_id']} ({task['task_id']})",
        body=(
            f"Environment: {task['environment']}\n"
            f"Description: {task['description']}\n"
            f"Please review and approve/reject."
        ),
    )


def notify_approved_stage(task: dict[str, Any], role: str, next_role: str | None) -> None:
    if next_role:
        _send(
            to=f"{next_role}@company.com",
            subject=f"Approval needed: {task['jira_id']} ({task['task_id']})",
            body=f"{role} has approved. Your approval is now required.",
        )
    else:
        _send(
            to="requester@company.com",
            subject=f"Request approved and running: {task['jira_id']}",
            body="All approvals complete. Orchestrator has started.",
        )


def notify_rejected(task: dict[str, Any], role: str, comment: str) -> None:
    _send(
        to="requester@company.com",
        subject=f"Request rejected: {task['jira_id']}",
        body=f"Rejected by {role}.\nReason: {comment}",
    )
