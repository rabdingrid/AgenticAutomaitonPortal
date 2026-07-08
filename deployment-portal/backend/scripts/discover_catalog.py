#!/usr/bin/env python3
"""
discover_catalog.py — list real Microservices / Portals / Utilities folders in the
release repo for a given branch, compare with the portal catalog, and optionally
apply fixes so validation paths resolve correctly.

Usage:
    GITSPACE_TOKEN=... python scripts/discover_catalog.py --branch R-2026-06-W4
    GITSPACE_TOKEN=... python scripts/discover_catalog.py --branch R-2026-06-W4 --apply

Options:
    --branch       Git ref (branch name), e.g. R-2026-06-W4
    --release      Release folder under each service (defaults to --branch)
    --project      Repo path (default: Titan/flpi-titan-release from config)
    --apply        Write reconciled names to config/catalog_services.json
    --json         Print machine-readable discovery report

Never stores the token. Read-only against GitSpace.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import catalog  # noqa: E402
import gitspace  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
CATALOG_JSON = BACKEND / "config" / "catalog_services.json"

_CATEGORY_BY_TYPE = {
    "microservice": "Microservices",
    "portal": "Portals",
    "utility": "Utilities",
}

_SECTION_FOLDERS = {
    "yaml": "YML",
    "db": "DB",
    "phrases": "Phrases",
}

_FILE_EXT = {
    "yaml": (".yml", ".yaml"),
    "db": (".sql",),
    "phrases": (".json",),
}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _folder_has_artifacts(
    client: gitspace.GitSpaceApiClient,
    project: str,
    branch: str,
    folder_path: str,
    section: str,
) -> bool:
    """True if the section folder exists and contains at least one real file."""
    entries = client.list_tree(project, branch, folder_path, recursive=True)
    exts = _FILE_EXT.get(section, ())
    for e in entries:
        if e.get("type") != "blob":
            continue
        name = e.get("name", "")
        if name.startswith("."):
            continue
        if not exts or any(name.lower().endswith(x) for x in exts):
            return True
    return False


def discover(
    client: gitspace.GitSpaceApiClient,
    project: str,
    branch: str,
    release: str,
) -> dict[str, Any]:
    """Scan the release repo and return services that have artifacts for each section."""
    found: dict[str, dict[str, list[str]]] = {
        "yaml": {"microservice": [], "portal": []},
        "db": {"microservice": []},
        "phrases": {"portal": []},
    }
    details: list[dict[str, Any]] = []

    for stype, category in _CATEGORY_BY_TYPE.items():
        top_entries = client.list_tree(project, branch, category, recursive=False)
        service_dirs = sorted(
            e.get("name", "") for e in top_entries if e.get("type") == "tree" and e.get("name")
        )
        for svc in service_dirs:
            release_root = f"{category}/{svc}/{release}"
            release_entries = client.list_tree(project, branch, release_root, recursive=False)
            subdirs = {
                e.get("name", "")
                for e in release_entries
                if e.get("type") == "tree" and e.get("name")
            }
            row: dict[str, Any] = {
                "type": stype,
                "category": category,
                "folder": svc,
                "release_path": release_root,
                "subdirs": sorted(subdirs),
                "sections": [],
            }
            if stype == "microservice":
                if "YML" in subdirs and _folder_has_artifacts(
                    client, project, branch, f"{release_root}/YML", "yaml"
                ):
                    found["yaml"]["microservice"].append(svc)
                    row["sections"].append("yaml")
                if "DB" in subdirs and _folder_has_artifacts(
                    client, project, branch, f"{release_root}/DB", "db"
                ):
                    found["db"]["microservice"].append(svc)
                    row["sections"].append("db")
            elif stype == "portal":
                if "YML" in subdirs and _folder_has_artifacts(
                    client, project, branch, f"{release_root}/YML", "yaml"
                ):
                    found["yaml"]["portal"].append(svc)
                    row["sections"].append("yaml")
                if "Phrases" in subdirs and _folder_has_artifacts(
                    client, project, branch, f"{release_root}/Phrases", "phrases"
                ):
                    found["phrases"]["portal"].append(svc)
                    row["sections"].append("phrases")
            if row["sections"]:
                details.append(row)

    return {"found": found, "details": details}


def _load_current_catalog() -> dict[str, dict[str, list[str]]]:
    if CATALOG_JSON.exists():
        with CATALOG_JSON.open(encoding="utf-8") as f:
            return json.load(f)
    return json.loads(json.dumps(catalog._DEFAULT_SECTION_SERVICE_NAMES))


def _pascal(label: str) -> str:
    return gitspace._pascal(label)


def compare_catalog(
    current: dict[str, dict[str, list[str]]],
    discovered: dict[str, dict[str, list[str]]],
) -> dict[str, Any]:
    """Diff catalog labels vs discovered folder names (PascalCase match)."""
    report: dict[str, Any] = {"sections": {}, "suggested_fixes": {}}

    checks = [
        ("yaml", "microservice"),
        ("yaml", "portal"),
        ("db", "microservice"),
        ("phrases", "portal"),
    ]
    for section, stype in checks:
        catalog_names = current.get(section, {}).get(stype, [])
        folder_names = discovered.get(section, {}).get(stype, [])
        folder_by_norm = {_norm(f): f for f in folder_names}

        matched: list[dict[str, str]] = []
        missing_in_repo: list[str] = []
        wrong_label: list[dict[str, str]] = []
        extra_in_repo: list[str] = []

        seen_folders: set[str] = set()
        for name in catalog_names:
            pn = _norm(_pascal(name))
            if pn in folder_by_norm:
                real = folder_by_norm[pn]
                seen_folders.add(real)
                if real != name and real != _pascal(name):
                    wrong_label.append({"catalog": name, "repo_folder": real})
                else:
                    matched.append({"catalog": name, "repo_folder": real})
            else:
                # fuzzy: prefix/substring match
                fuzzy = next(
                    (f for f in folder_names if pn in _norm(f) or _norm(f) in pn),
                    None,
                )
                if fuzzy:
                    seen_folders.add(fuzzy)
                    wrong_label.append({"catalog": name, "repo_folder": fuzzy, "note": "fuzzy"})
                else:
                    missing_in_repo.append(name)

        for f in folder_names:
            if f not in seen_folders:
                extra_in_repo.append(f)

        report["sections"][f"{section}/{stype}"] = {
            "matched": matched,
            "wrong_label": wrong_label,
            "missing_in_repo": missing_in_repo,
            "extra_in_repo": extra_in_repo,
            "discovered_count": len(folder_names),
            "catalog_count": len(catalog_names),
        }
        if folder_names:
            report["suggested_fixes"][f"{section}/{stype}"] = folder_names

    return report


def apply_catalog(
    current: dict[str, dict[str, list[str]]],
    diff: dict[str, Any],
) -> dict[str, dict[str, list[str]]]:
    """Merge discovery into catalog without dropping services missing on one release.

    - Renames catalog labels that don't match repo folders (wrong_label fixes)
    - Appends repo folders not yet in the catalog (extra_in_repo)
    - Keeps catalog entries not found on this release (they may exist on other releases)
  """
    out = json.loads(json.dumps(current))
    for key, sec in diff["sections"].items():
        section, stype = key.split("/", 1)
        names: list[str] = list(out.get(section, {}).get(stype, []))
        for w in sec.get("wrong_label", []):
            old, new = w["catalog"], w["repo_folder"]
            if old in names:
                names[names.index(old)] = new
            elif new not in names:
                names.append(new)
        for e in sec.get("extra_in_repo", []):
            if e not in names:
                names.append(e)
        if names:
            out.setdefault(section, {})[stype] = names
    return out


def _print_report(
    branch: str,
    release: str,
    project: str,
    discovery: dict[str, Any],
    diff: dict[str, Any],
) -> None:
    print(f"Project : {project}")
    print(f"Branch  : {branch}")
    print(f"Release : {release}")
    print("=" * 72)
    print("DISCOVERED (folders with real artifacts on this release)")
    for section, by_type in discovery["found"].items():
        for stype, names in by_type.items():
            if names:
                print(f"  {section}/{stype}: {len(names)} services")
                for n in names:
                    print(f"    • {n}")
    print()
    print("CATALOG vs REPO")
    print("-" * 72)
    for key, sec in diff["sections"].items():
        print(f"\n{key}")
        if sec["wrong_label"]:
            print("  WRONG LABEL (catalog name ≠ repo folder):")
            for w in sec["wrong_label"]:
                note = f" [{w['note']}]" if w.get("note") else ""
                print(f"    catalog `{w['catalog']}`  →  repo `{w['repo_folder']}`{note}")
        if sec["missing_in_repo"]:
            print("  IN CATALOG BUT NOT ON REPO (for this release):")
            for m in sec["missing_in_repo"]:
                print(f"    • {m}")
        if sec["extra_in_repo"]:
            print("  ON REPO BUT NOT IN CATALOG (will be added with --apply):")
            for e in sec["extra_in_repo"]:
                print(f"    • {e}")
        if not sec["wrong_label"] and not sec["missing_in_repo"] and not sec["extra_in_repo"]:
            print("  ✅ all catalog names match repo folders")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover release-repo service folders from GitSpace.")
    parser.add_argument("--branch", required=True, help="Git branch ref, e.g. R-2026-06-W4")
    parser.add_argument("--release", help="Release folder under each service (default: same as --branch)")
    parser.add_argument("--project", help="Project path (default: release_repo from config)")
    parser.add_argument("--apply", action="store_true", help="Write config/catalog_services.json")
    parser.add_argument("--json", action="store_true", help="Print JSON report to stdout")
    args = parser.parse_args()

    token = os.getenv("GITSPACE_TOKEN")
    if not token:
        print("ERROR: set GITSPACE_TOKEN in the environment.", file=sys.stderr)
        return 1

    cfg = gitspace.load_config()
    project = args.project or cfg.get("release_repo", "Titan/flpi-titan-release")
    release = args.release or args.branch
    base_url = cfg.get("base_url", "https://gitspace.foreverliving.com")
    api_path = cfg.get("api_path", "/api/v4")

    client = gitspace.GitSpaceApiClient(base_url, token, api_path=api_path, allow_write=False)
    if not client.branch_exists(project, args.branch):
        print(f"ERROR: branch `{args.branch}` not found in `{project}`.", file=sys.stderr)
        return 1

    discovery = discover(client, project, args.branch, release)
    current = _load_current_catalog()
    diff = compare_catalog(current, discovery["found"])

    if args.json:
        print(json.dumps({"discovery": discovery, "diff": diff, "current": current}, indent=2))
        if args.apply:
            merged = apply_catalog(current, diff)
            CATALOG_JSON.parent.mkdir(parents=True, exist_ok=True)
            with CATALOG_JSON.open("w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2)
                f.write("\n")
            print(f"Wrote {CATALOG_JSON}", file=sys.stderr)
        return 0

    _print_report(args.branch, release, project, discovery, diff)

    if args.apply:
        merged = apply_catalog(current, diff)
        CATALOG_JSON.parent.mkdir(parents=True, exist_ok=True)
        with CATALOG_JSON.open("w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
            f.write("\n")
        print(f"✅ Applied → {CATALOG_JSON}")
        print("   Restart the backend (or wait for --reload) so catalog picks up changes.")
    else:
        print("Dry run only. Re-run with --apply to write config/catalog_services.json")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
