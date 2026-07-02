"""
mock_executor.py

Simulates real execution: merge, Jenkins build, health polling, AI verification.
Every function here has the same signature it will have when backed by real
integrations (GitSpace API, Jenkins, API gateway). Swap the internals only —
callers never change.

Called by:
  - POST /sub-tasks/{id}/tick  (frontend polls this while a sub-task is running)

All randomness is seeded off stable identifiers so a demo is repeatable:
the same sub-task always fails/succeeds on the same step between runs.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

# Mock configuration — realistic timings and outcomes.
MOCK_HEALTH_POLL_ATTEMPTS = 3       # polls before a service reports "up"
MOCK_FAILURE_RATE = 0.08            # ~8% of sub-tasks fail on a middle step
MOCK_JENKINS_BASE_BUILD_NUMBER = 1280


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stable_seed(*parts: str) -> int:
    """Process-independent seed (unlike the built-in hash())."""
    return sum(ord(c) for c in "".join(parts))


def tick_sub_task(sub_task: dict[str, Any]) -> dict[str, Any]:
    """
    Called on each poll. Advances the sub-task one step forward.

    Returns {"step_advanced": bool, "new_step_status": str,
             "completed": bool, "failed": bool, "detail": str}

    This is the ONLY function the frontend needs to call to simulate progress.
    Never call step-specific helpers directly from the frontend.
    """
    steps = sub_task["steps"]
    current_step = next((s for s in steps if s["status"] == "running"), None)
    next_step = next((s for s in steps if s["status"] == "queued"), None)

    # Nothing running yet — start the first step.
    if current_step is None and next_step is not None:
        next_step["status"] = "running"
        next_step["ts"] = _now()
        return {"step_advanced": True, "new_step_status": "running",
                "completed": False, "failed": False,
                "detail": f"Started: {next_step['label']}"}

    if current_step is None:
        return {"step_advanced": False, "new_step_status": "done",
                "completed": True, "failed": False, "detail": "All steps complete"}

    step_idx = next(i for i, s in enumerate(steps) if s["step_id"] == current_step["step_id"])
    detail = _mock_step_detail(sub_task, current_step, step_idx)

    # Deterministic failure injection on a middle step only (demo safety).
    if MOCK_FAILURE_RATE > 0 and 0 < step_idx < len(steps) - 1:
        seed = _stable_seed(sub_task["sub_task_id"], current_step["step_id"])
        if random.Random(seed).random() < MOCK_FAILURE_RATE:
            current_step["status"] = "failed"
            current_step["detail"] = f"MOCK ERROR: {detail}"
            current_step["ts"] = _now()
            return {"step_advanced": True, "new_step_status": "failed",
                    "completed": False, "failed": True,
                    "detail": current_step["detail"]}

    # Complete the current step and advance to the next.
    current_step["status"] = "done"
    current_step["detail"] = detail
    current_step["ts"] = _now()

    if next_step:
        next_step["status"] = "running"
        next_step["ts"] = _now()
        return {"step_advanced": True, "new_step_status": "running",
                "completed": False, "failed": False, "detail": detail}

    return {"step_advanced": True, "new_step_status": "done",
            "completed": True, "failed": False, "detail": "Sub-task complete"}


def _mock_step_detail(sub_task: dict[str, Any], step: dict[str, Any], step_idx: int) -> str:
    """Generate realistic detail text for each step type."""
    label = sub_task.get("label", "service")
    service_key = sub_task.get("service_key", "service")
    release_branch = sub_task.get("release_branch") or "release/2026-07"
    jenkins_job = sub_task.get("jenkins_job") or "Titan-Microservices"
    build_num = sub_task.get("mock_build_number") or (
        MOCK_JENKINS_BASE_BUILD_NUMBER + _stable_seed(service_key) % 100
    )
    sha = f"a3f{_stable_seed(service_key) % 10000:04x}"
    section = sub_task["section"]

    if section == "build":
        details = [
            "GitSpace API: MR is approved and pipeline green",
            f"Merged into {release_branch} — commit sha: {sha}",
            f"Jenkins #{build_num} triggered — job: {jenkins_job}",
            f"Jenkins #{build_num}: tests passed (142/142), Docker image built, pushed to ECR",
            f"API gateway /health → HTTP 200 — service responding (attempt {MOCK_HEALTH_POLL_ATTEMPTS}/{MOCK_HEALTH_POLL_ATTEMPTS})",
            f"Ollama verified: 'Build #{build_num} succeeded. No errors in deployment logs. Service healthy.'",
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
    """
    Called by POST /tasks/validate to return per-link validation results.

    Returns a list of {"service_key", "label", "section", "valid", "errors"}.

    The real version would call GitSpace API, a YAML parser, a SQL linter, etc.
    Results are deterministic: same service_key + section → same outcome.
    """
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
