#!/usr/bin/env python3
"""
list_unmapped_repos.py — show build-catalog services that still use the
auto-guessed repo path (no entry in config/gitlab_repos.json).

Usage:
    python scripts/list_unmapped_repos.py

Add missing rows to config/gitlab_repos.json, then restart the backend.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import catalog
import gitspace


def _guessed_path(service: dict) -> str:
    cfg = gitspace.load_config()
    group = cfg.get("default_group", "Titan")
    name = gitspace._pascal(service.get("label") or "")
    stype = service.get("type", "")
    if stype == "microservice":
        suffix = cfg.get("microservice_repo_suffix", "Service")
        if suffix and not name.endswith(suffix):
            name = f"{name}{suffix}"
    return f"{group}/{name}"


def main() -> int:
    repos = gitspace.load_gitlab_repos()
    print("Build services — repo mapping status")
    print("=" * 72)
    missing = 0
    for stype in ("microservice", "portal", "utility"):
        for svc in catalog.load_section_services("build").get(stype, []):
            key = svc["key"]
            mapped = gitspace.service_repo_path(svc)
            resolved = gitspace.service_project_path(svc)
            if key in repos or mapped:
                print(f"  [mapped] {key}")
                print(f"           label={svc['label']!r}  repo={resolved}")
            else:
                missing += 1
                print(f"  [GUESS ] {key}")
                print(f"           label={svc['label']!r}  repo={resolved}  (auto)")
    print("=" * 72)
    print(f"{missing} service(s) still using auto-guessed repo paths.")
    print("Edit backend/config/gitlab_repos.json to add explicit mappings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
