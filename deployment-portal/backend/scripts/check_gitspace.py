#!/usr/bin/env python3
"""
check_gitspace.py — verify a GitSpace (GitLab) token can do the read-only
validation work: identify the user, confirm a branch exists, and read a file.

Usage:
    GITSPACE_TOKEN=xxxxx python scripts/check_gitspace.py <project> <branch> [file_path]

Examples:
    GITSPACE_TOKEN=... python scripts/check_gitspace.py Titan/flpi-titan-release main
    GITSPACE_TOKEN=... python scripts/check_gitspace.py Titan/flpi-titan-release release/2026.07 SomeService/release/2026.07/parameters.yml

Reads base_url / api_path from config/gitspace.json. Exit code 0 if all
requested checks pass, 1 otherwise. Never writes anything.
"""

from __future__ import annotations

import os
import sys

# Allow running from the backend dir or the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gitspace  # noqa: E402


def main() -> int:
    token = os.getenv("GITSPACE_TOKEN")
    if not token:
        print("ERROR: set GITSPACE_TOKEN in the environment first.")
        return 1
    if len(sys.argv) < 3:
        print(__doc__)
        return 1

    project, branch = sys.argv[1], sys.argv[2]
    file_path = sys.argv[3] if len(sys.argv) > 3 else ""

    cfg = gitspace.load_config()
    base_url = cfg.get("base_url", "https://gitspace.foreverliving.com")
    api_path = cfg.get("api_path", "/api/v4")
    client = gitspace.GitSpaceApiClient(base_url, token, api_path=api_path, allow_write=False)

    print(f"GitSpace : {base_url}{api_path}")
    print(f"Project  : {project}")
    print(f"Branch   : {branch}")
    print(f"File     : {file_path or '(none — skipping file read)'}")
    print("-" * 60)

    ok = True

    # 1) Token identity (proves read_user + auth works).
    r = client._get("/user")
    if r is not None and r.status_code == 200:
        u = r.json()
        print(f"[ OK ] Authenticated as: {u.get('username')} ({u.get('name')})")
    else:
        code = r.status_code if r is not None else "a network error"
        print(f"[FAIL] /user returned {code} — token invalid, VPN/proxy blocking, "
              "or base_url/api_path wrong.")
        ok = False

    # 2) Branch existence.
    if client.branch_exists(project, branch):
        print(f"[ OK ] Branch '{branch}' exists in '{project}'.")
    else:
        print(f"[FAIL] Branch '{branch}' NOT found in '{project}' "
              "(or no read access to this project).")
        ok = False

    # 3) File read — OR, if the path ends with '/', list the folder tree so you
    #    can discover the real file/folder names.
    if file_path.endswith("/") or file_path == "":
        entries = client.list_tree(project, branch, file_path.rstrip("/"), recursive=True)
        if entries:
            print(f"[ OK ] Listing '{file_path or '(repo root)'}' at '{branch}' "
                  f"({len(entries)} entries):")
            for e in sorted(entries, key=lambda x: (x.get("type") != "tree", x.get("path", ""))):
                marker = "📁" if e.get("type") == "tree" else "  •"
                print(f"        {marker} {e.get('path')}")
        else:
            print(f"[FAIL] Nothing found under '{file_path}' at '{branch}' "
                  "(empty, missing folder, or scope issue).")
            ok = False
    elif file_path:
        content = client.get_file(project, branch, file_path)
        if content is not None:
            preview = content[:200].replace("\n", "\\n")
            print(f"[ OK ] Read file ({len(content)} bytes). Preview: {preview}")
        else:
            print(f"[FAIL] Could not read '{file_path}' at '{branch}' "
                  "(missing file, or path/scope issue).")
            ok = False

    print("-" * 60)
    print("RESULT:", "read_api token is sufficient for validation ✅" if ok
          else "one or more checks failed ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
