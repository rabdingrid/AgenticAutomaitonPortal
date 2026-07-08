"""
notifications.py — email notifications on submit / approval.

When MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET, and MS_REFRESH_TOKEN are set,
sends real HTML mail via Microsoft Graph (delegated OAuth, /me/sendMail — same model
as n8n Outlook). Otherwise logs to notifications.log (mock mode).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from jose import JWTError, jwt

import graph_mail

LOG_PATH = Path(__file__).parent / "notifications.log"
_PORTAL_URL = os.getenv("PORTAL_BASE_URL", "http://localhost:5173").rstrip("/")
_API_URL = os.getenv("API_PUBLIC_URL", "http://localhost:9002").rstrip("/")
_MAIL_TOKEN_HOURS = int(os.getenv("MAIL_TOKEN_EXPIRE_HOURS", "72"))
_MAIL_SECRET = os.getenv("MAIL_ACTION_SECRET") or os.getenv("JWT_SECRET", "change-me-in-production-use-a-long-random-string")


def _parse_emails(raw: str) -> list[str]:
    return [e.strip() for e in (raw or "").split(",") if e.strip() and "@" in e]


def _log_mock(to: str, subject: str, body: str, cc: list[str] | None = None) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cc_line = f"CC: {', '.join(cc)}\n" if cc else ""
    entry = f"[{ts}] TO: {to}\n{cc_line}SUBJECT: {subject}\n{body}\n{'-' * 60}\n"
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(entry)
    print(f"[MOCK EMAIL] to={to} subject={subject}")
    for addr in cc or []:
        print(f"[MOCK EMAIL CC] to={addr} subject={subject}")


def _send_html(to: str, subject: str, html: str, *, cc: list[str] | None = None,
               plain: str = "") -> None:
    if graph_mail.is_configured():
        try:
            graph_mail.send_mail(to, subject, html, cc=cc, text_body=plain)
            print(f"[GRAPH EMAIL] to={to} subject={subject}")
            return
        except Exception as exc:  # noqa: BLE001
            print(f"[GRAPH EMAIL FAILED] {exc} — falling back to mock log")
    _log_mock(to, subject, plain or html, cc=cc)


def create_mail_action_token(task_id: str, action: str, *, email: str, role: str = "dev_lead") -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=_MAIL_TOKEN_HOURS)
    return jwt.encode(
        {"task_id": task_id, "action": action, "email": email, "role": role, "exp": exp},
        _MAIL_SECRET,
        algorithm="HS256",
    )


def verify_mail_action_token(token: str) -> dict[str, Any]:
    try:
        data = jwt.decode(token, _MAIL_SECRET, algorithms=["HS256"])
    except JWTError as exc:
        raise ValueError("Invalid or expired approval link") from exc
    if data.get("action") not in ("approve", "reject"):
        raise ValueError("Invalid action")
    return data


def _task_summary_lines(task: dict[str, Any]) -> list[str]:
    lines = [
        f"Request ID: {task.get('task_id', '')}",
        f"Jira: {task.get('jira_id', '')}",
        f"Environment: {task.get('environment', '')}",
        f"Requested by: {task.get('requested_by', '')}",
    ]
    if task.get("description"):
        lines.append(f"Description: {task['description']}")
    branches = task.get("branches") or []
    if branches:
        for i, b in enumerate(branches, 1):
            lines.append(f"Build branch {i}: {b.get('from', '')} → {b.get('to', '')}")
    elif task.get("branch_from") or task.get("branch_to"):
        lines.append(f"Branches: {task.get('branch_from', '')} → {task.get('branch_to', '')}")
    for job in task.get("jobs") or []:
        sec = job.get("section", "")
        links = ", ".join(
            (lnk.get("label") or lnk.get("service_key", "")) for lnk in job.get("links", [])
        )
        extra = ""
        if job.get("release_branch"):
            extra = f" (release: {job['release_branch']})"
        elif job.get("branch_from") or job.get("branch_to"):
            extra = f" ({job.get('branch_from', '')} → {job.get('branch_to', '')})"
        lines.append(f"  • {sec}: {links}{extra}")
    return lines


def _submitted_html(task: dict[str, Any], approver_name: str, approve_url: str, portal_url: str) -> str:
    facts = "".join(f"<li>{line}</li>" for line in _task_summary_lines(task))
    return f"""<!DOCTYPE html>
<html><body style="font-family:Segoe UI,Arial,sans-serif;color:#222;">
  <h2 style="color:#0b5cab;">Deployment approval needed</h2>
  <p>Hi {approver_name},</p>
  <p>A new deployment request needs your review as <strong>Development Lead</strong>.</p>
  <ul>{facts}</ul>
  <p style="margin:28px 0;">
    <a href="{approve_url}" style="background:#107c10;color:#fff;padding:12px 24px;
       text-decoration:none;border-radius:4px;font-weight:600;margin-right:12px;">Approve</a>
    <a href="{portal_url}" style="background:#0b5cab;color:#fff;padding:12px 24px;
       text-decoration:none;border-radius:4px;font-weight:600;">Review in portal</a>
  </p>
  <p style="font-size:12px;color:#666;">Reject with a comment in the portal. Approval links expire in {_MAIL_TOKEN_HOURS} hours.</p>
</body></html>"""


def notify_submitted(task: dict[str, Any]) -> None:
    from catalog import get_approver

    approver = get_approver(task.get("approver_key", ""))
    to = approver["email"] if approver and approver.get("email") else "dev-lead@company.com"
    approver_name = approver["name"] if approver else "Dev Lead"
    cc = _parse_emails(task.get("cc_emails", ""))

    task_id = task.get("task_id", "")
    token = create_mail_action_token(task_id, "approve", email=to, role="dev_lead")
    approve_url = f"{_API_URL}/mail/approve?token={quote(token)}"
    portal_url = f"{_PORTAL_URL}/tasks/{task_id}"

    subject = f"Approval needed: {task.get('jira_id', 'request')} ({task_id})"
    plain = "\n".join(_task_summary_lines(task)) + f"\n\nApprove: {approve_url}\nPortal: {portal_url}"
    html = _submitted_html(task, approver_name, approve_url, portal_url)

    _send_html(to, subject, html, cc=cc, plain=plain)


def notify_approved_stage(task: dict[str, Any], role: str, next_role: str | None) -> None:
    if next_role:
        _send_html(
            f"{next_role}@company.com",
            f"Approval needed: {task['jira_id']} ({task['task_id']})",
            f"<p>{role} has approved. Your approval is now required.</p>",
            plain=f"{role} has approved. Your approval is now required.",
        )
    else:
        _send_html(
            "requester@company.com",
            f"Request approved and running: {task['jira_id']}",
            "<p>All approvals complete. Orchestrator has started.</p>",
            plain="All approvals complete. Orchestrator has started.",
        )


def notify_rejected(task: dict[str, Any], role: str, comment: str) -> None:
    _send_html(
        "requester@company.com",
        f"Request rejected: {task['jira_id']}",
        f"<p>Rejected by {role}.</p><p>Reason: {comment}</p>",
        plain=f"Rejected by {role}.\nReason: {comment}",
    )
