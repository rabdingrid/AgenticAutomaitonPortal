"""
jenkins_client.py — Trigger Jenkins parameterized builds and poll console output.

Set JENKINS_ENABLED=true plus JENKINS_URL, JENKINS_USER, JENKINS_API_TOKEN in .env.
When disabled, callers fall back to mock_executor simulation.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any
from urllib.parse import urljoin

import httpx

_QUEUE_POLL_INTERVAL = 2.0
_QUEUE_POLL_MAX = 60


def is_enabled() -> bool:
    return os.getenv("JENKINS_ENABLED", "false").lower() in ("1", "true", "yes")


def _base_url() -> str:
    url = os.getenv("JENKINS_URL", "").rstrip("/")
    if not url:
        raise RuntimeError("JENKINS_URL is not set")
    return url


def _auth() -> tuple[str, str]:
    user = os.getenv("JENKINS_USER", "")
    token = os.getenv("JENKINS_API_TOKEN", "")
    if not user or not token:
        raise RuntimeError("JENKINS_USER and JENKINS_API_TOKEN are required")
    return user, token


def _format_trigger_error(status_code: int, body: str) -> str:
    title = re.search(r"<title>([^<]+)</title>", body, re.I)
    if title:
        return f"Jenkins trigger failed ({status_code}): {title.group(1).strip()}"
    return f"Jenkins trigger failed ({status_code}): {body[:500]}"


def job_url(job_path: str) -> str:
    """Project/Titan-Microservices → {base}/job/Project/job/Titan-Microservices"""
    parts = [p for p in job_path.strip("/").split("/") if p]
    return f"{_base_url()}/job/" + "/job/".join(parts)


def _crumb_headers(client: httpx.Client) -> dict[str, str]:
    try:
        r = client.get(f"{_base_url()}/crumbIssuer/api/json")
        if r.status_code == 200:
            data = r.json()
            return {data["crumbRequestField"]: data["crumb"]}
    except Exception:
        pass
    return {}


def trigger_build_with_parameters(job_path: str, params: dict[str, str]) -> dict[str, Any]:
    """Queue a parameterized build. Returns build_number and queue metadata."""
    url = f"{job_url(job_path)}/buildWithParameters"
    with httpx.Client(auth=_auth(), timeout=60.0, follow_redirects=False) as client:
        headers = _crumb_headers(client)
        r = client.post(url, data=params, headers=headers)
        if r.status_code not in (200, 201, 302):
            raise RuntimeError(_format_trigger_error(r.status_code, r.text))

        queue_url = r.headers.get("Location")
        if not queue_url:
            raise RuntimeError("Jenkins did not return a queue Location header")

        if not queue_url.startswith("http"):
            queue_url = urljoin(_base_url() + "/", queue_url.lstrip("/"))

        build_number = _wait_for_build_number(client, queue_url)
        return {
            "build_number": build_number,
            "queue_url": queue_url,
            "job_path": job_path,
            "job_url": job_url(job_path),
        }


def _wait_for_build_number(client: httpx.Client, queue_url: str) -> int:
    api_url = queue_url.rstrip("/") + "/api/json"
    for _ in range(_QUEUE_POLL_MAX):
        r = client.get(api_url)
        r.raise_for_status()
        data = r.json()
        executable = data.get("executable")
        if executable and executable.get("number"):
            return int(executable["number"])
        if data.get("cancelled"):
            raise RuntimeError("Jenkins queue item was cancelled")
        time.sleep(_QUEUE_POLL_INTERVAL)
    raise RuntimeError("Timed out waiting for Jenkins build number from queue")


def get_build_info(job_path: str, build_number: int) -> dict[str, Any]:
    """Returns Jenkins build state: status (running|success|failure|unknown) and raw result."""
    url = f"{job_url(job_path)}/{build_number}/api/json"
    with httpx.Client(auth=_auth(), timeout=30.0) as client:
        r = client.get(url)
        if r.status_code == 404:
            return {"status": "unknown", "result": None}
        r.raise_for_status()
        data = r.json()
        if data.get("building"):
            return {"status": "running", "result": None}
        result = data.get("result")
        if result == "SUCCESS":
            return {"status": "success", "result": result}
        if result in ("FAILURE", "ABORTED", "UNSTABLE"):
            return {"status": "failure", "result": result}
        return {"status": "unknown", "result": result}


def get_build_status(job_path: str, build_number: int) -> str:
    """Returns running | success | failure | unknown."""
    return get_build_info(job_path, build_number)["status"]


def count_active_builds(job_path: str) -> int:
    """How many builds of this job are live in Jenkins right now.

    Live = currently executing (``building``) + waiting in the build queue.
    This counts builds triggered by ANYONE (this portal, another portal
    instance, or a human in the Jenkins UI), so the orchestrator's slot gate
    can honour the real per-job concurrency limit and not just its own tally.

    Best-effort: returns 0 if Jenkins is disabled or unreachable so callers
    degrade to local-only slot counting instead of blocking forever.
    """
    if not is_enabled():
        return 0
    try:
        with httpx.Client(auth=_auth(), timeout=15.0) as client:
            return _count_building(client, job_path) + _count_queued(client, job_path)
    except Exception:
        return 0


def _count_building(client: httpx.Client, job_path: str) -> int:
    url = f"{job_url(job_path)}/api/json?tree=builds[number,building]{{0,25}}"
    r = client.get(url)
    r.raise_for_status()
    builds = r.json().get("builds", []) or []
    return sum(1 for b in builds if b.get("building"))


def _count_queued(client: httpx.Client, job_path: str) -> int:
    target = job_url(job_path).rstrip("/")
    r = client.get(f"{_base_url()}/queue/api/json?tree=items[task[url,name]]")
    r.raise_for_status()
    items = r.json().get("items", []) or []
    count = 0
    for item in items:
        task = item.get("task") or {}
        task_url = (task.get("url") or "").rstrip("/")
        if task_url == target:
            count += 1
    return count


def get_console_text(job_path: str, build_number: int, tail_lines: int = 200) -> str:
    url = f"{job_url(job_path)}/{build_number}/consoleText"
    with httpx.Client(auth=_auth(), timeout=60.0) as client:
        r = client.get(url)
        r.raise_for_status()
        text = r.text
        if tail_lines and len(text.splitlines()) > tail_lines:
            return "\n".join(text.splitlines()[-tail_lines:])
        return text
