#!/usr/bin/env python3
"""
trigger_build.py — Fire a Titan-Microservices or Titan-Portals build (no merge).

Build-only: blank MergeID, integ/integ, RELEASE_TAG optional on CLI.

Usage (from backend/):
    .venv/bin/python scripts/trigger_build.py <Service> [RELEASE_TAG]
    .venv/bin/python scripts/trigger_build.py --portal <Portal> [RELEASE_TAG]
    .venv/bin/python scripts/trigger_build.py microservice Shipping R-2026-07-W2
    .venv/bin/python scripts/trigger_build.py portal Account R-2026-07-W2

Examples:
    .venv/bin/python scripts/trigger_build.py Shipping R-2026-07-W1
    .venv/bin/python scripts/trigger_build.py --portal Account R-2026-07-W1
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import jenkins_client, jenkins_params  # noqa: E402


def _parse_args(argv: list[str]) -> tuple[str, str, str]:
    agent_type = "microservice"
    rest = list(argv)
    if rest and rest[0] == "--portal":
        agent_type = "portal"
        rest = rest[1:]
    elif rest and rest[0] in ("microservice", "portal"):
        agent_type = rest[0]
        rest = rest[1:]
    if not rest:
        print(
            "Usage: trigger_build.py [--portal | microservice|portal] <Name> [RELEASE_TAG]",
            file=sys.stderr,
        )
        sys.exit(2)
    name = rest[0]
    release_tag = rest[1] if len(rest) > 1 else ""
    return agent_type, name, release_tag


def main() -> int:
    agent_type, name, release_tag = _parse_args(sys.argv[1:])

    job_path = jenkins_params.job_path_for_agent(agent_type)
    params = jenkins_params.build_params_for_agent(
        agent_type=agent_type,
        service_label=name,
        release_tag=release_tag,
        merge_id="",
    )

    label = "Portal" if agent_type == "portal" else "Service"
    print(f"Type:   {agent_type}")
    print(f"Job:    {job_path}")
    print(f"{label}:   {name}")
    print(f"Params: {json.dumps(params, indent=2)}")

    if not jenkins_client.is_enabled():
        print(
            "\nJENKINS_ENABLED is not set — mock mode. "
            "Set JENKINS_ENABLED=true (+ JENKINS_URL/USER/API_TOKEN) to actually trigger."
        )
        return 0

    if not release_tag:
        print("\nWARNING: no RELEASE_TAG given — triggering with an empty tag.", file=sys.stderr)

    print("\nTriggering Jenkins build...")
    try:
        result = jenkins_client.trigger_build_with_parameters(job_path, params)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: trigger failed: {exc}", file=sys.stderr)
        return 1

    print(f"Queued build #{result['build_number']} — {result.get('job_url', job_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
