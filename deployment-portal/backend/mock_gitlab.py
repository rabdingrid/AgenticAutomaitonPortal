"""
mock_gitlab.py — Mock branch autocomplete (GitSpace stand-in).

Deterministic per project path so demos are repeatable, but NOT purely
random — every common query word is guaranteed to appear at least once,
so typing a partial branch name during a live demo never returns an
empty list.

Swap this file's internals for a real GitLab API call later — the
function signature `list_branches(project_path, query)` stays the same.
"""

from __future__ import annotations

import random

_BRANCH_PREFIXES = ["feature", "bugfix", "hotfix", "release"]
_BRANCH_WORDS = ["billing", "account", "login", "checkout", "refactor", "config", "audit", "migration"]


def list_branches(project_path: str, query: str = "") -> list[str]:
    seed = sum(ord(c) for c in project_path)
    rng = random.Random(seed)

    branches = ["develop", "main"]
    # Guarantee every common word appears at least once (demo-reliable).
    for word in _BRANCH_WORDS:
        prefix = rng.choice(_BRANCH_PREFIXES)
        suffix = rng.randint(100, 999)
        branches.append(f"{prefix}/{word}-{suffix}")

    if query:
        q = query.lower()
        branches = [b for b in branches if q in b.lower()]

    return sorted(set(branches))[:15]
