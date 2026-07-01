"""
gitspace.py — GitSpace (GitLab) integration layer.

Two responsibilities:

1. URL construction — turn the details a user filled in (service, release
   branch, source/target branch) into the real GitSpace URLs:
     - file blob URLs   (yaml / db / phrases land as files in the release repo)
     - merge-request URLs (build sections become merge requests on the service repo)
     - compare URLs

2. A pluggable client that talks to GitSpace. Today we ship `MockGitSpaceClient`
   (deterministic, no network — drives demos and the validation script). When the
   real integration lands, implement `GitSpaceApiClient` against the GitLab REST
   API and/or feed live data through the webhook endpoint in main.py. The rest of
   the app only depends on the `GitSpaceClient` interface, so nothing else changes.

URL templates and per-service repo paths live in config/gitspace.json so they can
be corrected without touching code.
"""

from __future__ import annotations

import hashlib
import json
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).parent / "config" / "gitspace.json"

_DEFAULT_CONFIG: dict[str, Any] = {
    "base_url": "https://gitspace.foreverliving.com",
    "default_group": "Titan",
    "release_repo": "Titan/flpi-titan-release",
    "templates": {
        "blob": "{base}/{project}/-/blob/{ref}/{path}?ref_type=heads",
        "merge_request": "{base}/{project}/-/merge_requests/{iid}",
        "merge_requests_index": "{base}/{project}/-/merge_requests",
        "compare": "{base}/{project}/-/compare/{from}...{to}",
        "tree": "{base}/{project}/-/tree/{ref}?ref_type=heads",
        "new_merge_request": "{base}/{project}/-/merge_requests/new",
    },
    "section_file_paths": {
        "phrases": "Portals/{service}/{release}/Phrases/Phrases.json",
        "yaml": "{service}/{release}/parameters.yml",
        "db": "{service}/{release}/changelog.sql",
    },
    "section_repo": {
        "phrases": "release_repo",
        "yaml": "release_repo",
        "db": "release_repo",
        "build": "service_repo",
    },
    "service_path_overrides": {},
}


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            json.dump(_DEFAULT_CONFIG, f, indent=2)
        return json.loads(json.dumps(_DEFAULT_CONFIG))
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Fill any missing keys from defaults so a partial config still works.
    for k, v in _DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


def _pascal(label: str) -> str:
    parts = re.split(r"[\s_\-]+", label.strip())
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def service_project_path(service: dict[str, Any], cfg: dict[str, Any] | None = None) -> str:
    """Real GitSpace repo path for a service, e.g. 'Titan/ReportingService'.

    Resolution order: explicit override by service key → default group + PascalCase
    label. (The synthetic catalog `gitlab_project_path` is intentionally ignored
    here because it does not match GitSpace's real group/repo layout.)
    """
    cfg = cfg or load_config()
    key = service.get("key", "")
    override = cfg.get("service_path_overrides", {}).get(key)
    if override:
        return override
    group = cfg.get("default_group", "Titan")
    return f"{group}/{_pascal(service.get('label') or service.get('key') or 'Unknown')}"


def _repo_for_section(section: str, service_path: str, cfg: dict[str, Any]) -> str:
    which = cfg.get("section_repo", {}).get(section, "service_repo")
    return cfg["release_repo"] if which == "release_repo" else service_path


def file_path_for(section: str, service: dict[str, Any], release: str, cfg: dict[str, Any]) -> str | None:
    tmpl = cfg.get("section_file_paths", {}).get(section)
    if not tmpl:
        return None
    return tmpl.format(service=service.get("label", service.get("key", "")), release=release or "")


def blob_url(section: str, service: dict[str, Any], release: str, cfg: dict[str, Any] | None = None) -> str | None:
    cfg = cfg or load_config()
    path = file_path_for(section, service, release, cfg)
    if not path:
        return None
    project = _repo_for_section(section, service_project_path(service, cfg), cfg)
    return cfg["templates"]["blob"].format(base=cfg["base_url"], project=project, ref=release or "main", path=path)


def merge_request_url(service: dict[str, Any], iid: int, cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or load_config()
    project = service_project_path(service, cfg)
    return cfg["templates"]["merge_request"].format(base=cfg["base_url"], project=project, iid=iid)


def compare_url(service: dict[str, Any], from_ref: str, to_ref: str, cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or load_config()
    project = service_project_path(service, cfg)
    return cfg["templates"]["compare"].format(
        base=cfg["base_url"], project=project, **{"from": from_ref, "to": to_ref}
    )


# ──────────────────────────────────────────────────────────────────────────
# Client interface
# ──────────────────────────────────────────────────────────────────────────

class GitSpaceClient(ABC):
    @abstractmethod
    def get_file(self, project: str, ref: str, path: str) -> str | None: ...

    @abstractmethod
    def compare(self, project: str, from_ref: str, to_ref: str) -> dict[str, Any]: ...

    @abstractmethod
    def create_merge_request(self, project: str, source: str, target: str, title: str) -> dict[str, Any]: ...


def _seed(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest(), 16)


# Sample file bodies the mock "fetches" so the AI/structural checks have
# something realistic to analyse. A branch/path containing "broken" or
# "conflict" deliberately yields a faulty payload to exercise the error path.
_SAMPLE_YAML_OK = "server:\n  port: 8080\nspring:\n  datasource:\n    url: jdbc:postgresql://db/app\nfeature:\n  newCheckout: true\n"
_SAMPLE_YAML_BAD = "server:\n  port: 8080\n  port: 9090\nspring:\n    datasource\n  url jdbc:bad\n"
_SAMPLE_SQL_OK = "--liquibase formatted sql\n--changeset titan:2026-07-w1\nALTER TABLE account ADD COLUMN loyalty_tier VARCHAR(32);\n"
_SAMPLE_SQL_BAD = "--changeset (missing author/id)\nALTER TABLE account ADD COLUMN loyalty_tier VARCHAR(32)\nDROP TABLE orders;\n"
_SAMPLE_PHRASES_OK = '{\n  "welcome.title": "Welcome",\n  "checkout.button": "Place order"\n}\n'
_SAMPLE_PHRASES_BAD = '{\n  "welcome.title": "Welcome",\n  "checkout.button": "Place order",\n}\n'


class MockGitSpaceClient(GitSpaceClient):
    """Deterministic, offline stand-in. Repeatable per (project, ref, path).

    Scenario control for the validation script / demos: include the word
    "broken" in a file path or "conflict" in a branch name to force failures.
    """

    def get_file(self, project: str, ref: str, path: str) -> str | None:
        faulty = "broken" in path.lower() or "broken" in ref.lower()
        low = path.lower()
        if low.endswith(".sql"):
            return _SAMPLE_SQL_BAD if faulty else _SAMPLE_SQL_OK
        if "phrases" in low or low.endswith(".json"):
            return _SAMPLE_PHRASES_BAD if faulty else _SAMPLE_PHRASES_OK
        if low.endswith((".yml", ".yaml")):
            return _SAMPLE_YAML_BAD if faulty else _SAMPLE_YAML_OK
        # Missing file: ~1 in 12, seeded.
        if _seed(project, ref, path) % 12 == 0:
            return None
        return _SAMPLE_YAML_OK

    def compare(self, project: str, from_ref: str, to_ref: str) -> dict[str, Any]:
        if not from_ref or not to_ref:
            return {"mergeable": False, "ahead": 0, "behind": 0, "conflicts": [],
                    "reason": "Missing source or target branch"}
        if "conflict" in f"{from_ref}{to_ref}".lower():
            return {"mergeable": False, "ahead": 3, "behind": 2,
                    "conflicts": ["src/config/parameters.yml", "README.md"],
                    "reason": "Merge conflicts must be resolved"}
        seed = _seed(project, from_ref, to_ref)
        conflict = seed % 9 == 0  # ~11% seeded conflict rate
        if conflict:
            return {"mergeable": False, "ahead": seed % 7 + 1, "behind": seed % 4,
                    "conflicts": ["src/main/resources/application.yml"],
                    "reason": "Merge conflicts must be resolved"}
        return {"mergeable": True, "ahead": seed % 9 + 1, "behind": 0, "conflicts": [], "reason": "Fast-forward / clean merge"}

    def create_merge_request(self, project: str, source: str, target: str, title: str) -> dict[str, Any]:
        cmp = self.compare(project, source, target)
        iid = _seed(project, source, target) % 9000 + 100
        return {
            "iid": iid,
            "title": title,
            "source_branch": source,
            "target_branch": target,
            "state": "opened",
            "mergeable": cmp["mergeable"],
            "conflicts": cmp["conflicts"],
            "has_conflicts": not cmp["mergeable"],
            "detail": cmp.get("reason", ""),
        }


_CLIENT: GitSpaceClient | None = None


def get_client() -> GitSpaceClient:
    """Single seam to swap in GitSpaceApiClient later (or wire webhook data)."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = MockGitSpaceClient()
    return _CLIENT


def set_client(client: GitSpaceClient) -> None:
    global _CLIENT
    _CLIENT = client
