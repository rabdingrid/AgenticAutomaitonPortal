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


def _parse_emails(raw: str) -> list[str]:
    return [e.strip() for e in (raw or "").split(",") if e.strip() and "@" in e]


def _send(to: str, subject: str, body: str, cc: list[str] | None = None) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cc_line = f"CC: {', '.join(cc)}\n" if cc else ""
    entry = f"[{ts}] TO: {to}\n{cc_line}SUBJECT: {subject}\n{body}\n{'-' * 60}\n"
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(entry)
    print(f"[MOCK EMAIL] to={to} subject={subject}")
    for addr in cc or []:
        print(f"[MOCK EMAIL CC] to={addr} subject={subject}")


def notify_submitted(task: dict[str, Any]) -> None:
    from catalog import get_approver

    approver = get_approver(task.get("approver_key", ""))
    to = approver["email"] if approver and approver.get("email") else "dev-lead@company.com"
    cc = _parse_emails(task.get("cc_emails", ""))
    _send(
        to=to,
        subject=f"Approval needed: {task['jira_id']} ({task['task_id']})",
        body=(
            f"Environment: {task['environment']}\n"
            f"Description: {task['description']}\n"
            f"Please review and approve/reject."
        ),
        cc=cc,
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
