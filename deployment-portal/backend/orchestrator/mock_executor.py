"""
mock_executor.py

Simulates real execution: merge, Jenkins build, health polling, AI verification.
When JENKINS_ENABLED=true, microservice and portal build steps trigger and poll
the real Titan-Microservices / Titan-Portals jobs using jenkins_params from the sub-task.

Called by POST /sub-tasks/{id}/tick (frontend polls while a sub-task is running).
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

from orchestrator import jenkins_client

MOCK_HEALTH_POLL_ATTEMPTS = 3
MOCK_FAILURE_RATE = 0.08
MOCK_JENKINS_BASE_BUILD_NUMBER = 1280


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stable_seed(*parts: str) -> int:
    return sum(ord(c) for c in "".join(parts))


def _is_jenkins_build(sub_task: dict[str, Any]) -> bool:
    return (
        sub_task.get("section") == "build"
        and sub_task.get("sub_type") in ("microservice", "portal")
    )


def _step_label(step: dict[str, Any]) -> str:
    return step.get("label") or ""


def _is_trigger_step(step: dict[str, Any]) -> bool:
    return "Trigger Jenkins" in _step_label(step)


def _is_wait_step(step: dict[str, Any]) -> bool:
    return "Waiting for Jenkins build" in _step_label(step)


def tick_sub_task(sub_task: dict[str, Any]) -> dict[str, Any]:
    steps = sub_task["steps"]
    current_step = next((s for s in steps if s["status"] == "running"), None)
    next_step = next((s for s in steps if s["status"] == "queued"), None)

    if current_step is None and next_step is not None:
        next_step["status"] = "running"
        next_step["ts"] = _now()
        return {
            "step_advanced": True,
            "new_step_status": "running",
            "completed": False,
            "failed": False,
            "detail": f"Started: {next_step['label']}",
        }

    if current_step is None:
        return {
            "step_advanced": False,
            "new_step_status": "done",
            "completed": True,
            "failed": False,
            "detail": "All steps complete",
        }

    # Real Jenkins: poll until build finishes on the wait step.
    if (
        jenkins_client.is_enabled()
        and _is_jenkins_build(sub_task)
        and _is_wait_step(current_step)
        and sub_task.get("jenkins_build_number")
    ):
        job_path = sub_task.get("jenkins_job") or ""
        build_num = int(sub_task["jenkins_build_number"])
        try:
            build_status = jenkins_client.get_build_status(job_path, build_num)
        except Exception as exc:
            return {
                "step_advanced": False,
                "new_step_status": "running",
                "completed": False,
                "failed": False,
                "detail": f"Jenkins poll error: {exc}",
            }
        if build_status == "running":
            return {
                "step_advanced": False,
                "new_step_status": "running",
                "completed": False,
                "failed": False,
                "detail": f"Jenkins #{build_num} still running…",
            }
        if build_status == "failure":
            current_step["status"] = "failed"
            current_step["detail"] = f"Jenkins #{build_num} failed"
            current_step["ts"] = _now()
            extra_logs = _fetch_console_tail(sub_task, build_num)
            return {
                "step_advanced": True,
                "new_step_status": "failed",
                "completed": False,
                "failed": True,
                "detail": current_step["detail"],
                "extra_logs": extra_logs,
            }
        if build_status == "success":
            extra_logs = _fetch_console_tail(sub_task, build_num)
            current_step["status"] = "done"
            current_step["detail"] = f"Jenkins #{build_num} succeeded"
            current_step["ts"] = _now()
            if next_step:
                next_step["status"] = "running"
                next_step["ts"] = _now()
            return {
                "step_advanced": True,
                "new_step_status": "running" if next_step else "done",
                "completed": not next_step,
                "failed": False,
                "detail": current_step["detail"],
                "extra_logs": extra_logs,
                "jenkins_build_number": build_num,
            }

    step_idx = next(i for i, s in enumerate(steps) if s["step_id"] == current_step["step_id"])
    detail = _mock_step_detail(sub_task, current_step, step_idx)
    jenkins_build_number = None
    jenkins_triggered = None
    extra_logs: list[str] = []

    # Real Jenkins: trigger on the "Trigger Jenkins" step.
    if (
        jenkins_client.is_enabled()
        and _is_jenkins_build(sub_task)
        and _is_trigger_step(current_step)
        and not sub_task.get("jenkins_triggered")
    ):
        job_path = sub_task.get("jenkins_job") or ""
        params = sub_task.get("jenkins_params") or {}
        try:
            result = jenkins_client.trigger_build_with_parameters(job_path, params)
            jenkins_build_number = result["build_number"]
            jenkins_triggered = True
            detail = (
                f"Jenkins #{jenkins_build_number} triggered — "
                f"{result.get('job_url', job_path)} "
                f"(RELEASE_TAG={params.get('RELEASE_TAG', '')})"
            )
        except Exception as exc:
            current_step["status"] = "failed"
            current_step["detail"] = f"Jenkins trigger failed: {exc}"
            current_step["ts"] = _now()
            return {
                "step_advanced": True,
                "new_step_status": "failed",
                "completed": False,
                "failed": True,
                "detail": current_step["detail"],
            }

    if MOCK_FAILURE_RATE > 0 and 0 < step_idx < len(steps) - 1 and not jenkins_client.is_enabled():
        seed = _stable_seed(sub_task["sub_task_id"], current_step["step_id"])
        if random.Random(seed).random() < MOCK_FAILURE_RATE:
            current_step["status"] = "failed"
            current_step["detail"] = f"MOCK ERROR: {detail}"
            current_step["ts"] = _now()
            return {
                "step_advanced": True,
                "new_step_status": "failed",
                "completed": False,
                "failed": True,
                "detail": current_step["detail"],
            }

    current_step["status"] = "done"
    current_step["detail"] = detail
    current_step["ts"] = _now()

    if next_step:
        next_step["status"] = "running"
        next_step["ts"] = _now()
        result: dict[str, Any] = {
            "step_advanced": True,
            "new_step_status": "running",
            "completed": False,
            "failed": False,
            "detail": detail,
        }
    else:
        result = {
            "step_advanced": True,
            "new_step_status": "done",
            "completed": True,
            "failed": False,
            "detail": "Sub-task complete",
        }

    if jenkins_build_number is not None:
        result["jenkins_build_number"] = jenkins_build_number
    if jenkins_triggered is not None:
        result["jenkins_triggered"] = jenkins_triggered
    if extra_logs:
        result["extra_logs"] = extra_logs
    return result


def _fetch_console_tail(sub_task: dict[str, Any], build_num: int) -> list[str]:
    job_path = sub_task.get("jenkins_job") or ""
    try:
        text = jenkins_client.get_console_text(job_path, build_num, tail_lines=30)
        return [line for line in text.splitlines() if line.strip()][-10:]
    except Exception:
        return []


def _mock_step_detail(sub_task: dict[str, Any], step: dict[str, Any], step_idx: int) -> str:
    label = sub_task.get("label", "service")
    service_key = sub_task.get("service_key", "service")
    release_branch = sub_task.get("release_branch") or "release/2026-07"
    jenkins_job = sub_task.get("jenkins_job") or "Titan-Microservices"
    build_num = sub_task.get("jenkins_build_number") or sub_task.get("mock_build_number") or (
        MOCK_JENKINS_BASE_BUILD_NUMBER + _stable_seed(service_key) % 100
    )
    params = sub_task.get("jenkins_params") or {}
    sha = f"a3f{_stable_seed(service_key) % 10000:04x}"
    section = sub_task["section"]

    if section == "build":
        release_tag = params.get("RELEASE_TAG", "")
        details = [
            "GitSpace API: MR is approved and pipeline green",
            f"Merged into {release_branch} — commit sha: {sha}",
            f"Jenkins #{build_num} triggered — job: {jenkins_job}",
            f"Jenkins #{build_num}: tests passed (142/142), Docker image built, pushed to ECR",
            f"API gateway /health → HTTP 200 — service responding (attempt {MOCK_HEALTH_POLL_ATTEMPTS}/{MOCK_HEALTH_POLL_ATTEMPTS})",
            (
                f"Ollama verified: Build #{build_num} succeeded"
                + (f" (RELEASE_TAG={release_tag})" if release_tag else "")
                + ". Service healthy."
            ),
        ]
    elif section == "yaml":
        details = [
            f"YAML syntax valid — no parse errors in {label}",
            "Schema validation passed — all required keys present",
            f"Jenkins yml_automation_2.0 #{build_num} triggered",
            f"Build #{build_num} complete — config applied",
            "Config endpoint returned expected values",
            "Config propagation confirmed across all instances",
        ]
    elif section == "db":
        details = [
            "SQL syntax valid — no DROP/TRUNCATE/DELETE without WHERE detected",
            "Liquibase changeset format valid — 3 changesets found",
            "Liquibase update: 3/3 changesets applied successfully",
            "DB migration verified — schema matches expected state",
            "Smoke query returned expected result set",
        ]
    elif section == "phrases":
        details = [
            "Phrase file format valid — 47 keys found",
            "All keys present in master schema",
            "Phrase deployment job triggered",
            "CDN cache invalidated — phrases propagated to 3/3 regions",
        ]
    else:
        details = [f"Step {step_idx + 1} complete"]

    return details[step_idx] if step_idx < len(details) else "Step complete"


def mock_validate_section(section: str, links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for link in links:
        service_key = link.get("service_key", "")
        label = link.get("label", service_key)
        errors: list[str] = []

        if not service_key:
            errors.append("No service selected")
        else:
            seed = _stable_seed(service_key, section)
            rng = random.Random(seed)

            if section == "build":
                if rng.random() < 0.10:
                    errors.append(
                        f"MR !{2800 + rng.randint(1, 99)}: merge conflicts detected — resolve before submitting"
                    )
                elif rng.random() < 0.05:
                    errors.append(f"GitSpace pipeline not green for {label} — 2 tests failing")
            elif section == "yaml":
                if rng.random() < 0.08:
                    errors.append(f"YAML syntax error in {label}: unexpected token at line 14")
            elif section == "db":
                if rng.random() < 0.12:
                    errors.append(f"SQL risk detected in {label}: DELETE statement without WHERE clause")
            elif section == "phrases":
                if rng.random() < 0.06:
                    errors.append(f"Missing required phrase key 'checkout.error.retry' in {label}")

        results.append({
            "service_key": service_key,
            "label": label,
            "section": section,
            "valid": len(errors) == 0,
            "errors": errors,
        })

    return results
