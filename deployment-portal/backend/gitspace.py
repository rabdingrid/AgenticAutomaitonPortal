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
import os
import re
import sys
import urllib.parse
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx

CONFIG_PATH = Path(__file__).parent / "config" / "gitspace.json"
GITLAB_REPOS_PATH = Path(__file__).parent / "config" / "gitlab_repos.json"

_repo_map_cache: dict[str, Any] | None = None

_DEFAULT_CONFIG: dict[str, Any] = {
    "base_url": "https://gitspace.foreverliving.com",
    # "mock" = offline deterministic stand-in; "live" = real GitSpace REST API.
    # Can be overridden with env GITSPACE_MODE.
    "mode": "mock",
    # GitLab REST prefix under base_url.
    "api_path": "/api/v4",
    # Only create merge requests when true (needs an `api`/write-scoped token).
    # With a read-only `read_api` token, leave this false. Env: GITSPACE_ALLOW_WRITE.
    "allow_write": False,
    "default_group": "Titan",
    "release_repo": "Titan/flpi-titan-release",
    "templates": {
        "blob": "{base}/{project}/-/blob/{ref}/{path}?ref_type=heads",
        "merge_request": "{base}/{project}/-/merge_requests/{iid}",
        "merge_requests_index": "{base}/{project}/-/merge_requests",
        "compare": "{base}/{project}/-/compare/{target}...{source}",
        "tree": "{base}/{project}/-/tree/{ref}?ref_type=heads",
        "new_merge_request": "{base}/{project}/-/merge_requests/new",
    },
    # Top-level folder in the release repo, chosen by service type.
    "category_by_type": {
        "microservice": "Microservices",
        "portal": "Portals",
        "utility": "Utilities",
    },
    # Real layout: {category}/{ServicePascalCase}/{release}/{SectionFolder}/{file}
    # e.g. Microservices/CustomLinkGeneratorService/R-2026-06-W4/YML/parameters.yml
    # These are the *representative* single paths (used for blob URLs and the
    # offline mock). Live validation scans `section_dirs` for the real files.
    "section_file_paths": {
        "yaml": "{category}/{service}/{release}/YML/parameters.yml",
        "db": "{category}/{service}/{release}/DB/Common.sql",
        "phrases": "{category}/{service}/{release}/Phrases/Phrases.json",
    },
    # Folder that holds each section's artifacts; the live client lists it and
    # validates every file whose extension matches `section_file_ext`.
    "section_dirs": {
        "yaml": "{category}/{service}/{release}/YML",
        "db": "{category}/{service}/{release}/DB",
        "phrases": "{category}/{service}/{release}/Phrases",
    },
    "section_file_ext": {
        "yaml": [".yml", ".yaml"],
        "db": [".sql"],
        "phrases": [".json"],
    },
    "section_repo": {
        "phrases": "release_repo",
        "yaml": "release_repo",
        "db": "release_repo",
        "build": "service_repo",
    },
    # GitLab repo suffix for build/MR checks (service repos, not release-repo folders).
    "microservice_repo_suffix": "Service",
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


def _parse_repo_entry(entry: Any) -> tuple[str | None, int | None]:
    """Return (repo_path, project_id) from a gitlab_repos.json value."""
    if isinstance(entry, str):
        return entry.strip() or None, None
    if isinstance(entry, dict):
        path = entry.get("repo_path") or entry.get("path")
        pid = entry.get("project_id")
        return (str(path).strip() if path else None,
                int(pid) if pid is not None else None)
    return None, None


def load_gitlab_repos() -> dict[str, Any]:
    """Load service_key → repo mapping from config/gitlab_repos.json."""
    global _repo_map_cache
    if _repo_map_cache is not None:
        return _repo_map_cache
    merged: dict[str, Any] = {}
    if GITLAB_REPOS_PATH.exists():
        with GITLAB_REPOS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        merged.update(data.get("repos", {}))
    # Legacy overrides in gitspace.json still work.
    merged.update(load_config().get("service_path_overrides", {}))
    _repo_map_cache = merged
    return merged


def reload_gitlab_repos() -> None:
    """Clear cached repo map (e.g. after editing gitlab_repos.json)."""
    global _repo_map_cache
    _repo_map_cache = None


def service_repo_path(service: dict[str, Any], cfg: dict[str, Any] | None = None) -> str | None:
    """Mapped GitSpace repo path for this catalog service, if configured."""
    key = service.get("key", "")
    entry = load_gitlab_repos().get(key)
    path, _ = _parse_repo_entry(entry)
    return path


def service_project_id(service: dict[str, Any]) -> int | None:
    """GitLab numeric project id when present in gitlab_repos.json (for later API use)."""
    entry = load_gitlab_repos().get(service.get("key", ""))
    _, pid = _parse_repo_entry(entry)
    return pid


def service_project_path(service: dict[str, Any], cfg: dict[str, Any] | None = None) -> str:
    """GitSpace repo path for build/MR checks, e.g. 'Titan/AccountService'.

    Resolution order:
      1. config/gitlab_repos.json (by catalog service_key) — preferred
      2. Legacy service_path_overrides in gitspace.json
      3. Fallback: Titan/{PascalCase label}[Service] for microservices

    Release-repo YAML/DB/Phrases paths are separate (section_dir_for).
    """
    cfg = cfg or load_config()
    mapped = service_repo_path(service, cfg)
    if mapped:
        return mapped
    group = cfg.get("default_group", "Titan")
    name = _pascal(service.get("label") or service.get("key") or "Unknown")
    stype = service.get("type", "")
    if stype == "microservice":
        suffix = cfg.get("microservice_repo_suffix", "Service")
        if suffix and not name.endswith(suffix):
            name = f"{name}{suffix}"
    return f"{group}/{name}"


def _repo_for_section(section: str, service_path: str, cfg: dict[str, Any]) -> str:
    which = cfg.get("section_repo", {}).get(section, "service_repo")
    return cfg["release_repo"] if which == "release_repo" else service_path


def file_path_for(section: str, service: dict[str, Any], release: str, cfg: dict[str, Any]) -> str | None:
    tmpl = cfg.get("section_file_paths", {}).get(section)
    if not tmpl:
        return None
    category = cfg.get("category_by_type", {}).get(service.get("type", ""), "")
    svc = _pascal(service.get("label") or service.get("key") or "")
    return tmpl.format(category=category, service=svc, release=release or "")


def section_dir_for(section: str, service: dict[str, Any], release: str, cfg: dict[str, Any]) -> str | None:
    tmpl = cfg.get("section_dirs", {}).get(section)
    if not tmpl:
        return None
    category = cfg.get("category_by_type", {}).get(service.get("type", ""), "")
    svc = _pascal(service.get("label") or service.get("key") or "")
    return tmpl.format(category=category, service=svc, release=release or "")


def service_root_dir(service: dict[str, Any], cfg: dict[str, Any]) -> str:
    """Parent folder for all release cycles of a service, e.g. Microservices/Account."""
    category = cfg.get("category_by_type", {}).get(service.get("type", ""), "")
    svc = _pascal(service.get("label") or service.get("key") or "")
    return f"{category}/{svc}"


def previous_release_label(release: str) -> str | None:
    """Best-effort previous release folder name for baseline comparison."""
    if not release:
        return None
    m = re.match(r"^(R-\d{4}-\d{2}-W)(\d+)$", release, re.IGNORECASE)
    if m:
        n = int(m.group(2))
        return f"{m.group(1)}{n - 1}" if n > 1 else None
    m2 = re.match(r"^(release/)(\d{4})-(\d{2})$", release, re.IGNORECASE)
    if m2:
        year, month = int(m2.group(2)), int(m2.group(3))
        if month > 1:
            return f"{m2.group(1)}{year}-{month - 1:02d}"
        return f"{m2.group(1)}{year - 1}-12"
    return None


def swap_release_in_path(file_path: str, old_release: str, new_release: str) -> str:
    """Replace the release folder segment inside a repo file path."""
    if old_release in file_path:
        return file_path.replace(f"/{old_release}/", f"/{new_release}/", 1)
    return file_path


def resolve_baseline_release(
    client: "GitSpaceClient",
    project: str,
    ref: str,
    service: dict[str, Any],
    current_release: str,
    cfg: dict[str, Any],
) -> str | None:
    """Pick the prior release folder to diff against (new vs old artifact check).

    Resolution order:
      1. Sibling folders under {category}/{Service} on the release branch (live)
      2. Heuristic previous label (R-2026-06-W4 → R-2026-06-W3)
      3. Env VALIDATION_BASELINE_RELEASE override (same for all services)
    """
    override = (os.getenv("VALIDATION_BASELINE_RELEASE") or "").strip()
    if override and override != current_release:
        return override

    root = service_root_dir(service, cfg)
    siblings = [
        e.get("name", "")
        for e in client.list_tree(project, ref, root, recursive=False)
        if e.get("type") == "tree" and e.get("name") and e.get("name") != current_release
    ]
    if siblings:
        older = sorted(s for s in siblings if s < current_release)
        if older:
            return older[-1]
        return sorted(siblings)[-1]

    guessed = previous_release_label(current_release)
    if guessed and guessed != current_release:
        return guessed
    return None


def baseline_blob_url(
    section: str,
    service: dict[str, Any],
    baseline_release: str,
    file_path: str,
    current_release: str,
    cfg: dict[str, Any],
) -> str | None:
    """GitSpace blob URL for the baseline (old) copy of an artifact."""
    cfg = cfg or load_config()
    baseline_path = swap_release_in_path(file_path, current_release, baseline_release)
    project = _repo_for_section(section, service_project_path(service, cfg), cfg)
    return cfg["templates"]["blob"].format(
        base=cfg["base_url"], project=project, ref=baseline_release, path=baseline_path
    )


_DB_ENV_FILES: dict[str, list[str]] = {
    "INTEG": ["Common.sql", "INTEG.sql"],
    "UAT": ["Common.sql", "UAT.sql"],
    "PROD": ["Common.sql", "PROD.sql"],
}


def db_filenames_for_environment(environment: str) -> list[str]:
    """SQL files to merge for DB validation (Common + env-specific)."""
    env = (environment or "").strip().upper()
    return list(_DB_ENV_FILES.get(env, ["Common.sql"]))


def resolve_db_files(
    environment: str,
    service: dict[str, Any],
    release: str,
    cfg: dict[str, Any],
    client: "GitSpaceClient",
) -> list[str]:
    """Return full repo paths for env-aware DB files that exist on the release branch.

    Probes each candidate with ``get_file`` so only files that actually exist
    are included (mock and live)."""
    project = _repo_for_section("db", service_project_path(service, cfg), cfg)
    folder = section_dir_for("db", service, release, cfg)
    if not folder or not release:
        return []
    paths: list[str] = []
    for fname in db_filenames_for_environment(environment):
        fp = f"{folder}/{fname}"
        if client.get_file(project, release, fp) is not None:
            paths.append(fp)
    return paths


def resolve_section_files(section: str, service: dict[str, Any], release: str,
                          cfg: dict[str, Any], client: "GitSpaceClient") -> list[str]:
    """Real artifact file paths for a section.

    Live: list the section folder and keep files whose extension matches
    `section_file_ext` (ignoring `.gitkeep`/placeholders). Falls back to the
    single representative template path when listing yields nothing (or for the
    offline mock, whose `list_tree` returns [])."""
    project = _repo_for_section(section, service_project_path(service, cfg), cfg)
    folder = section_dir_for(section, service, release, cfg)
    exts = [e.lower() for e in cfg.get("section_file_ext", {}).get(section, [])]
    if folder and release:
        entries = client.list_tree(project, release, folder, recursive=False)
        files = [
            e.get("path", "") for e in entries
            if e.get("type") == "blob"
            and not e.get("name", "").startswith(".")
            and (not exts or any(e.get("name", "").lower().endswith(x) for x in exts))
        ]
        if files:
            return sorted(files)
    # Offline mock falls back to a template path. Live GitSpace must not guess —
    # if nothing is on the release branch, validation reports "not on release".
    if isinstance(client, MockGitSpaceClient):
        single = file_path_for(section, service, release, cfg)
        return [single] if single else []
    return []


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


def compare_url(
    service: dict[str, Any],
    source_ref: str,
    target_ref: str,
    cfg: dict[str, Any] | None = None,
) -> str:
    """GitLab/GitSpace compare link: ``target...source`` (merge target first, then source).

    Form ``From`` = source branch, ``To`` = target branch — same as an MR.
    """
    cfg = cfg or load_config()
    project = service_project_path(service, cfg)
    return cfg["templates"]["compare"].format(
        base=cfg["base_url"], project=project, target=target_ref, source=source_ref
    )


# ──────────────────────────────────────────────────────────────────────────
# Client interface
# ──────────────────────────────────────────────────────────────────────────

class GitSpaceClient(ABC):
    @abstractmethod
    def branch_exists(self, project: str, ref: str) -> bool: ...

    @abstractmethod
    def get_file(self, project: str, ref: str, path: str, *, artifact_role: str = "new") -> str | None: ...

    @abstractmethod
    def compare(self, project: str, source_ref: str, target_ref: str) -> dict[str, Any]: ...

    @abstractmethod
    def create_merge_request(self, project: str, source: str, target: str, title: str) -> dict[str, Any]: ...

    def list_tree(self, project: str, ref: str, path: str = "", recursive: bool = True) -> list[dict[str, Any]]:
        """List repo entries under `path`. Default: unsupported (empty) so
        callers fall back to a single templated path (e.g. the offline mock)."""
        return []


def _seed(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest(), 16)


# Sample file bodies the mock "fetches" so the AI/structural checks have
# something realistic to analyse. A branch/path containing "broken" or
# "conflict" deliberately yields a faulty payload to exercise the error path.
_SAMPLE_YAML_OLD = (
    "server:\n  port: 8080\nspring:\n  datasource:\n    url: jdbc:postgresql://db/app\n"
    "feature:\n  newCheckout: false\n"
)
_SAMPLE_YAML_OK = (
    "server:\n  port: 9090\nspring:\n  datasource:\n    url: jdbc:postgresql://db/app\n"
    "feature:\n  newCheckout: true\n"
)
_SAMPLE_YAML_BAD = "server:\n  port: 8080\n  port: 9090\nspring:\n    datasource\n  url jdbc:bad\n"
def _service_folder_from_path(path: str) -> str:
    """Extract service folder name from a release-repo artifact path."""
    parts = [p for p in path.split("/") if p]
    for i, p in enumerate(parts):
        if p in ("Microservices", "Portals", "Utilities") and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _mock_db_files_for_folder(folder_path: str) -> list[str]:
    """Which SQL blobs exist under this mock DB folder (mirrors typical repo layout)."""
    service = _service_folder_from_path(folder_path).lower()
    files = ["Common.sql"]
    # Account commonly ships Common.sql only; other services often add env SQL.
    if service not in ("account",):
        files.extend(["INTEG.sql", "UAT.sql", "PROD.sql"])
    return files


def _mock_sql_content(service: str, fname: str) -> str:
    """Per-service SQL samples so validation output differs by microservice."""
    svc = service or "Service"
    slug = re.sub(r"service$", "", svc, flags=re.IGNORECASE).lower() or "app"
    base_id = (_seed(svc, "db") % 80) + 1
    if fname.lower() == "common.sql":
        return (
            "--liquibase formatted sql\n"
            f"-- Changeset pganesan:{base_id} labels:Approved\n"
            f"ALTER TABLE {slug} ADD COLUMN loyalty_tier VARCHAR(32);\n"
            f"-- Changeset devteam:{base_id + 1} labels:Pending\n"
            f"ALTER TABLE {slug} ADD COLUMN draft_flag BOOLEAN;\n"
        )
    if fname.lower() == "integ.sql":
        return (
            "--liquibase formatted sql\n"
            f"-- Changeset pganesan:{base_id + 10} labels:Approved\n"
            f"ALTER TABLE {slug} ADD COLUMN integ_only_col VARCHAR(16);\n"
        )
    if fname.lower() == "uat.sql":
        return (
            "--liquibase formatted sql\n"
            f"-- Changeset pganesan:{base_id + 20} labels:Approved\n"
            f"ALTER TABLE {slug} ADD COLUMN uat_only_col VARCHAR(16);\n"
        )
    if fname.lower() == "prod.sql":
        return (
            "--liquibase formatted sql\n"
            f"-- Changeset pganesan:{base_id + 30} labels:Approved\n"
            f"ALTER TABLE {slug} ADD COLUMN prod_only_col VARCHAR(16);\n"
        )
    return (
        "--liquibase formatted sql\n"
        f"-- Changeset pganesan:{base_id} labels:Approved\n"
        f"ALTER TABLE {slug} ADD COLUMN col VARCHAR(8);\n"
    )


_SAMPLE_SQL_BAD = (
    "--changeset badformat:1 labels:Approved\n"
    "ALTER TABLE account ADD COLUMN loyalty_tier VARCHAR(32);\n"
)
_SAMPLE_SQL_DUP = (
    "--liquibase formatted sql\n"
    "-- Changeset pganesan:1 labels:Approved\n"
    "ALTER TABLE account ADD COLUMN a VARCHAR(1);\n"
    "-- Changeset pganesan:1 labels:Approved\n"
    "ALTER TABLE account ADD COLUMN b VARCHAR(1);\n"
)
_SAMPLE_PHRASES_OK = '{\n  "welcome.title": "Welcome",\n  "checkout.button": "Place order"\n}\n'
_SAMPLE_PHRASES_BAD = '{\n  "welcome.title": "Welcome",\n  "checkout.button": "Place order",\n}\n'


class MockGitSpaceClient(GitSpaceClient):
    """Deterministic, offline stand-in. Repeatable per (project, ref, path).

    Scenario control for the validation script / demos: include the word
    "broken" in a file path or "conflict" in a branch name to force failures.
    """

    def branch_exists(self, project: str, ref: str) -> bool:
        if not ref:
            return False
        # Scenario control: a branch containing "missing"/"nobranch" is absent.
        return not any(tok in ref.lower() for tok in ("missing", "nobranch", "does-not-exist"))

    def list_tree(self, project: str, ref: str, path: str = "", recursive: bool = True) -> list[dict[str, Any]]:
        if recursive:
            return []
        parts = [p for p in (path or "").split("/") if p]
        # Service root: list release folder siblings for baseline discovery.
        if len(parts) == 2 and parts[0] in ("Microservices", "Portals", "Utilities"):
            prev = previous_release_label(ref)
            entries: list[dict[str, Any]] = []
            if prev:
                entries.append({"name": prev, "type": "tree", "path": f"{path}/{prev}"})
            if ref:
                entries.append({"name": ref, "type": "tree", "path": f"{path}/{ref}"})
            return entries
        # Section folder: return mock artifact files.
        if path.endswith("/YML"):
            return [{"name": "parameters.yml", "type": "blob", "path": f"{path}/parameters.yml"}]
        if path.endswith("/DB"):
            return [
                {"name": n, "type": "blob", "path": f"{path}/{n}"}
                for n in _mock_db_files_for_folder(path)
            ]
        if path.endswith("/Phrases"):
            return [{"name": "Phrases.json", "type": "blob", "path": f"{path}/Phrases.json"}]
        return []

    def get_file(self, project: str, ref: str, path: str, *, artifact_role: str = "new") -> str | None:
        faulty = "broken" in path.lower() or "broken" in ref.lower()
        conflict = "conflict" in ref.lower() or "conflict" in path.lower()
        low = path.lower()
        if low.endswith(".sql"):
            if faulty:
                return _SAMPLE_SQL_BAD
            if "dup" in ref.lower() or "duplicate" in path.lower():
                return _SAMPLE_SQL_DUP
            if conflict:
                return (
                    "--liquibase formatted sql\n"
                    "-- Changeset pganesan:99 labels:Approved\n"
                    "DROP TABLE orders;\n"
                )
            fname = path.rsplit("/", 1)[-1]
            folder = path.rsplit("/", 1)[0]
            if fname not in _mock_db_files_for_folder(folder):
                return None
            service = _service_folder_from_path(path)
            return _mock_sql_content(service, fname)
        if "phrases" in low or low.endswith(".json"):
            return _SAMPLE_PHRASES_BAD if faulty else _SAMPLE_PHRASES_OK
        if low.endswith((".yml", ".yaml")):
            if faulty:
                return _SAMPLE_YAML_BAD
            baseline = artifact_role == "old"
            return _SAMPLE_YAML_OLD if baseline else _SAMPLE_YAML_OK
        # Missing file: ~1 in 12, seeded.
        if _seed(project, ref, path) % 12 == 0:
            return None
        baseline = artifact_role == "old"
        return _SAMPLE_YAML_OLD if baseline else _SAMPLE_YAML_OK

    def compare(self, project: str, source_ref: str, target_ref: str) -> dict[str, Any]:
        if not source_ref or not target_ref:
            return {"mergeable": False, "ahead": 0, "behind": 0, "conflicts": [],
                    "reason": "Missing source or target branch"}
        if "conflict" in f"{source_ref}{target_ref}".lower():
            return {"mergeable": False, "ahead": 3, "behind": 2,
                    "conflicts": ["src/config/parameters.yml", "README.md"],
                    "reason": "Merge conflicts must be resolved"}
        seed = _seed(project, source_ref, target_ref)
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


class GitSpaceApiClient(GitSpaceClient):
    """Live, read-first GitSpace (GitLab) REST client.

    Only needs a **read_api** (or read_repository) token for the review use-case:
    verify branches exist, read the YAML/DB/phrases artifacts, and diff branches.
    Merge-request *creation* is a write op and stays disabled unless `allow_write`
    is set (which requires an `api`-scoped token) — so today we never POST.

    The token is read from env `GITSPACE_TOKEN` (preferred) and never persisted.
    Projects are addressed by URL-encoded full path (e.g. 'Titan/flpi-titan-release'),
    so no numeric project IDs are required.
    """

    def __init__(self, base_url: str, token: str, *, api_path: str = "/api/v4",
                 allow_write: bool = False, timeout: float = 20.0,
                 trust_env: bool = False) -> None:
        self.api = base_url.rstrip("/") + api_path
        self._headers = {"PRIVATE-TOKEN": token}
        self.allow_write = allow_write
        self.timeout = timeout
        # GitSpace is an internal host reachable directly over the VPN. By
        # default ignore HTTP(S)_PROXY env vars, which a corporate VPN sets and
        # which would otherwise route this internal call through the proxy and
        # fail. Set GITSPACE_TRUST_ENV=1 if a proxy really is required.
        self.trust_env = trust_env

    @staticmethod
    def _enc(value: str) -> str:
        return urllib.parse.quote(value, safe="")

    def _client(self) -> httpx.Client:
        return httpx.Client(trust_env=self.trust_env, timeout=self.timeout)

    def _get(self, endpoint: str, **params: Any) -> httpx.Response | None:
        try:
            with self._client() as c:
                return c.get(f"{self.api}{endpoint}", headers=self._headers,
                             params={k: v for k, v in params.items() if v is not None})
        except Exception:
            return None

    def branch_exists(self, project: str, ref: str) -> bool:
        if not ref:
            return False
        r = self._get(f"/projects/{self._enc(project)}/repository/branches/{self._enc(ref)}")
        return bool(r and r.status_code == 200)

    def get_file(self, project: str, ref: str, path: str, *, artifact_role: str = "new") -> str | None:
        if not path:
            return None
        r = self._get(
            f"/projects/{self._enc(project)}/repository/files/{self._enc(path)}/raw",
            ref=ref or "main",
        )
        if r is not None and r.status_code == 200:
            return r.text
        return None

    def list_tree(self, project: str, ref: str, path: str = "", recursive: bool = True) -> list[dict[str, Any]]:
        """List repository entries under `path` on `ref`. Returns GitLab tree
        objects: {id, name, type: 'blob'|'tree', path, mode}. Paginated."""
        entries: list[dict[str, Any]] = []
        page = 1
        while page and page < 50:  # hard cap to avoid runaways
            r = self._get(f"/projects/{self._enc(project)}/repository/tree",
                          ref=ref, path=path or None,
                          recursive="true" if recursive else "false",
                          per_page=100, page=page)
            if r is None or r.status_code != 200:
                break
            batch = r.json()
            if not batch:
                break
            entries.extend(batch)
            nxt = r.headers.get("X-Next-Page", "")
            page = int(nxt) if nxt.isdigit() else 0
        return entries

    def compare(self, project: str, source_ref: str, target_ref: str) -> dict[str, Any]:
        if not source_ref or not target_ref:
            return {"mergeable": None, "ahead": 0, "behind": 0, "conflicts": [],
                    "reason": "Missing source or target branch"}
        # GitLab API: commits on source not in target → from=target, to=source
        r = self._get(f"/projects/{self._enc(project)}/repository/compare",
                      **{"from": target_ref, "to": source_ref})
        if r is not None and r.status_code == 200:
            commits = r.json().get("commits") or []
            return {"mergeable": None, "ahead": len(commits), "behind": 0, "conflicts": [],
                    "reason": "Read-only compare — mergeability not evaluated (no write scope)."}
        code = r.status_code if r is not None else "network error"
        return {"mergeable": None, "ahead": 0, "behind": 0, "conflicts": [],
                "reason": f"Compare unavailable (HTTP {code})."}

    def create_merge_request(self, project: str, source: str, target: str, title: str) -> dict[str, Any]:
        # Read-only mode: never POST. Just confirm both branches exist so the
        # build section still yields a useful (non-writing) result.
        if not self.allow_write:
            return {
                "iid": 0, "title": title, "source_branch": source, "target_branch": target,
                "state": "not_created", "mergeable": None, "conflicts": [], "has_conflicts": False,
                "source_exists": self.branch_exists(project, source) if source else False,
                "target_exists": self.branch_exists(project, target) if target else False,
                "detail": "MR creation disabled (read-only token); branch existence verified.",
            }
        # Write path (enabled later with an `api`-scoped token).
        try:
            with self._client() as c:
                r = c.post(f"{self.api}/projects/{self._enc(project)}/merge_requests",
                           headers=self._headers,
                           json={"source_branch": source, "target_branch": target, "title": title})
        except Exception as e:  # noqa: BLE001
            return {"iid": 0, "state": "error", "mergeable": None, "conflicts": [],
                    "has_conflicts": False, "source_branch": source, "target_branch": target,
                    "detail": f"MR create error: {e}"}
        if r.status_code in (200, 201):
            d = r.json()
            return {"iid": d.get("iid", 0), "title": d.get("title", title),
                    "source_branch": source, "target_branch": target,
                    "state": d.get("state", "opened"),
                    "mergeable": d.get("merge_status") == "can_be_merged",
                    "conflicts": [], "has_conflicts": bool(d.get("has_conflicts", False)),
                    "detail": d.get("merge_status", "")}
        return {"iid": 0, "state": "error", "mergeable": None, "conflicts": [],
                "has_conflicts": False, "source_branch": source, "target_branch": target,
                "detail": f"MR create failed (HTTP {r.status_code})."}


_CLIENT: GitSpaceClient | None = None


def _build_client() -> GitSpaceClient:
    cfg = load_config()
    mode = (os.getenv("GITSPACE_MODE") or cfg.get("mode", "mock")).lower()
    token = os.getenv("GITSPACE_TOKEN") or cfg.get("token") or ""
    if mode == "live":
        if not token:
            print("[gitspace] GITSPACE_MODE=live but no token found "
                  "(set GITSPACE_TOKEN) — falling back to mock client.", file=sys.stderr)
            return MockGitSpaceClient()
        allow_write = (os.getenv("GITSPACE_ALLOW_WRITE", "").lower() in ("1", "true", "yes")
                       or bool(cfg.get("allow_write", False)))
        trust_env = (os.getenv("GITSPACE_TRUST_ENV", "").lower() in ("1", "true", "yes")
                     or bool(cfg.get("trust_env", False)))
        return GitSpaceApiClient(
            cfg.get("base_url", _DEFAULT_CONFIG["base_url"]), token,
            api_path=cfg.get("api_path", "/api/v4"), allow_write=allow_write,
            trust_env=trust_env,
        )
    return MockGitSpaceClient()


def get_client() -> GitSpaceClient:
    """Single seam: returns the live API client when GITSPACE_MODE=live and a
    token is present, otherwise the offline mock. Cached per-process."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = _build_client()
    return _CLIENT


def set_client(client: GitSpaceClient) -> None:
    global _CLIENT
    _CLIENT = client
