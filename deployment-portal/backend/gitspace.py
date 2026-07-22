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
RELEASE_FOLDER_MAP_PATH = Path(__file__).parent / "config" / "gitspace_release_folder_map.json"

_repo_map_cache: dict[str, Any] | None = None
_release_folder_map_cache: dict[str, dict[str, str]] | None = None

PHRASES_FOLDER_BY_KIND: dict[str, str] = {
    "phrases": "Phrases",
    "schemaforms": "SchemaForms",
    "newschemaforms": "NewSchemaForms",
}

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


def load_release_folder_map() -> dict[str, dict[str, str]]:
    """Catalog label → release-repo folder name, by service type."""
    global _release_folder_map_cache
    if _release_folder_map_cache is not None:
        return _release_folder_map_cache
    merged: dict[str, dict[str, str]] = {"microservice": {}, "portal": {}, "utility": {}}
    if RELEASE_FOLDER_MAP_PATH.exists():
        with RELEASE_FOLDER_MAP_PATH.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        for bucket in ("microservice", "portal", "utility"):
            merged[bucket] = {
                k: v for k, v in (raw.get(bucket) or {}).items() if not str(k).startswith("_")
            }
    _release_folder_map_cache = merged
    return merged


def map_release_folder_name(service: dict[str, Any]) -> str:
    """Resolve GitSpace release-branch folder from catalog label."""
    label = (service.get("label") or service.get("key") or "").strip()
    if not label:
        return "Unknown"
    stype = service.get("type") or "microservice"
    bucket = load_release_folder_map().get(stype) or {}
    if label in bucket:
        return bucket[label]
    if service.get("type") == "portal":
        return label
    return _pascal(label)


def _release_folder_name(service: dict[str, Any]) -> str:
    """Folder name under Microservices/Portals on the release branch."""
    return map_release_folder_name(service)


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
    svc = _release_folder_name(service)
    return tmpl.format(category=category, service=svc, release=release or "")


def section_dir_for(section: str, service: dict[str, Any], release: str, cfg: dict[str, Any]) -> str | None:
    category = cfg.get("category_by_type", {}).get(service.get("type", ""), "")
    svc = _release_folder_name(service)
    if section == "phrases":
        kind = (service.get("phrases_kind") or "phrases").lower()
        folder = PHRASES_FOLDER_BY_KIND.get(kind, "Phrases")
        return f"{category}/{svc}/{release}/{folder}"
    tmpl = cfg.get("section_dirs", {}).get(section)
    if not tmpl:
        return None
    return tmpl.format(category=category, service=svc, release=release or "")


def service_root_dir(service: dict[str, Any], cfg: dict[str, Any]) -> str:
    """Parent folder for all release cycles of a service, e.g. Microservices/Account."""
    category = cfg.get("category_by_type", {}).get(service.get("type", ""), "")
    svc = _release_folder_name(service)
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


def tree_url(project: str, ref: str, path: str = "", cfg: dict[str, Any] | None = None) -> str | None:
    """GitSpace folder tree URL for a path under a ref."""
    if not path:
        return None
    cfg = cfg or load_config()
    base = cfg["templates"]["tree"].format(
        base=cfg["base_url"], project=project, ref=ref or "main",
    )
    if "?ref_type=heads" in base:
        return base.replace("?ref_type=heads", f"/{path}?ref_type=heads")
    return f"{base}/{path}"


def merge_request_url(service: dict[str, Any], iid: int, cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or load_config()
    project = service_project_path(service, cfg)
    return cfg["templates"]["merge_request"].format(base=cfg["base_url"], project=project, iid=iid)


def merge_request_url_for_project(project: str, iid: int, cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or load_config()
    return cfg["templates"]["merge_request"].format(base=cfg["base_url"], project=project, iid=iid)


def summarize_changes(
    *,
    source_branch: str,
    target_branch: str,
    files_changed: list[str],
    commits_count: int = 0,
    has_conflicts: bool = False,
    preview: bool = True,
) -> str:
    """Short human summary (3–4 lines) for approvers."""
    lines: list[str] = []
    if has_conflicts:
        lines.append(
            f"Merge **{source_branch}** into **{target_branch}** has conflicts that must be resolved in GitSpace before approval."
        )
    elif commits_count == 0 and not files_changed:
        lines.append(f"Branches **{source_branch}** and **{target_branch}** are in sync — no file changes detected.")
    else:
        commit_part = f"{commits_count} commit(s)" if commits_count else "changes"
        file_part = f"{len(files_changed)} file(s)" if files_changed else "files"
        lines.append(
            f"Proposed merge **{source_branch} → {target_branch}**: {commit_part} across {file_part}."
        )
    if files_changed:
        preview = ", ".join(files_changed[:4])
        if len(files_changed) > 4:
            preview += f", … (+{len(files_changed) - 4} more)"
        lines.append(f"Changed paths: {preview}.")
    if not has_conflicts and (commits_count or files_changed):
        if preview:
            lines.append("No merge conflicts detected. An MR will be opened when the request is submitted.")
        else:
            lines.append("No merge conflicts detected. Merge request is open — Dev Lead approval will merge.")
    return "\n".join(lines[:4])


def _mergeable_from_status(merge_status: str | None, has_conflicts: bool | None) -> bool | None:
    if has_conflicts:
        return False
    if not merge_status:
        return None
    ms = merge_status.lower()
    if ms == "can_be_merged":
        return True
    if ms in ("cannot_be_merged", "cannot_be_merged_recheck"):
        return False
    return None


def compare_url_from_project(project: str, source_ref: str, target_ref: str, cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or load_config()
    return cfg["templates"]["compare"].format(
        base=cfg["base_url"], project=project, target=target_ref, source=source_ref,
    )


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

    def preview_merge(self, project: str, source: str, target: str) -> dict[str, Any]:
        """Check branches and mergeability without creating an MR (validate step)."""
        return self.create_merge_request(project, source, target, "preview")

    def get_merge_request(self, project: str, iid: int) -> dict[str, Any] | None:
        return None

    def accept_merge_request(self, project: str, iid: int) -> dict[str, Any]:
        return {"ok": False, "detail": "Merge not supported"}

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
        if path.endswith("/SchemaForms"):
            return [{"name": "SchemaForm.json", "type": "blob", "path": f"{path}/SchemaForm.json"}]
        if path.endswith("/NewSchemaForms"):
            return [{"name": "NewSchemaForm.json", "type": "blob", "path": f"{path}/NewSchemaForm.json"}]
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
        files = cmp.get("conflicts") or ["src/main/resources/application.yml"] if cmp.get("ahead") else []
        return {
            "iid": iid,
            "project": project,
            "title": title,
            "source_branch": source,
            "target_branch": target,
            "state": "opened",
            "merge_status": "can_be_merged" if cmp["mergeable"] else "cannot_be_merged",
            "mergeable": cmp["mergeable"],
            "conflicts": cmp["conflicts"],
            "has_conflicts": not cmp["mergeable"],
            "detail": cmp.get("reason", ""),
            "web_url": merge_request_url_for_project(project, iid),
            "files_changed": files if not cmp["mergeable"] else files,
            "commits_count": cmp.get("ahead", 0),
            "changes_summary": summarize_changes(
                source_branch=source,
                target_branch=target,
                files_changed=files,
                commits_count=cmp.get("ahead", 0),
                has_conflicts=not cmp["mergeable"],
            ),
            "created": True,
        }

    def preview_merge(self, project: str, source: str, target: str) -> dict[str, Any]:
        cmp = self.compare(project, source, target)
        files = cmp.get("conflicts") or (["src/config/parameters.yml"] if cmp.get("ahead") else [])
        return {
            "iid": 0,
            "project": project,
            "source_branch": source,
            "target_branch": target,
            "state": "preview",
            "merge_status": "can_be_merged" if cmp["mergeable"] else "cannot_be_merged",
            "mergeable": cmp["mergeable"],
            "conflicts": cmp["conflicts"],
            "has_conflicts": not cmp["mergeable"],
            "detail": cmp.get("reason", "Preview only — MR will be created on submit."),
            "web_url": compare_url_from_project(project, source, target),
            "files_changed": files,
            "commits_count": cmp.get("ahead", 0),
            "changes_summary": summarize_changes(
                source_branch=source,
                target_branch=target,
                files_changed=files,
                commits_count=cmp.get("ahead", 0),
                has_conflicts=not cmp["mergeable"],
            ),
            "created": False,
        }

    def get_merge_request(self, project: str, iid: int) -> dict[str, Any] | None:
        if not iid:
            return None
        return self.create_merge_request(project, "feature/mock", "integ", "mock")

    def accept_merge_request(self, project: str, iid: int) -> dict[str, Any]:
        return {"ok": True, "state": "merged", "iid": iid, "detail": f"MR !{iid} merged (mock)."}


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

    def _post(self, endpoint: str, json_body: dict[str, Any] | None = None) -> httpx.Response | None:
        try:
            with self._client() as c:
                return c.post(f"{self.api}{endpoint}", headers=self._headers, json=json_body or {})
        except Exception:
            return None

    def _put(self, endpoint: str, json_body: dict[str, Any] | None = None) -> httpx.Response | None:
        try:
            with self._client() as c:
                return c.put(f"{self.api}{endpoint}", headers=self._headers, json=json_body or {})
        except Exception:
            return None

    def _compare_details(self, project: str, source_ref: str, target_ref: str) -> dict[str, Any]:
        r = self._get(
            f"/projects/{self._enc(project)}/repository/compare",
            **{"from": target_ref, "to": source_ref},
        )
        if r is None or r.status_code != 200:
            code = r.status_code if r is not None else "network error"
            return {"commits_count": 0, "files_changed": [], "detail": f"Compare unavailable (HTTP {code})."}
        data = r.json()
        commits = data.get("commits") or []
        diffs = data.get("diffs") or []
        files = [d.get("new_path") or d.get("old_path") or "" for d in diffs if d.get("new_path") or d.get("old_path")]
        return {
            "commits_count": len(commits),
            "files_changed": files[:20],
            "detail": "",
        }

    def _find_open_mr(self, project: str, source: str, target: str) -> dict[str, Any] | None:
        r = self._get(
            f"/projects/{self._enc(project)}/merge_requests",
            state="opened",
            source_branch=source,
            target_branch=target,
        )
        if r is None or r.status_code != 200:
            return None
        items = r.json() or []
        return items[0] if items else None

    def _mr_changes(self, project: str, iid: int) -> list[str]:
        r = self._get(f"/projects/{self._enc(project)}/merge_requests/{iid}/changes")
        if r is None or r.status_code != 200:
            return []
        changes = r.json().get("changes") or []
        out: list[str] = []
        for ch in changes[:20]:
            path = ch.get("new_path") or ch.get("old_path") or ""
            if path:
                out.append(path)
        return out

    def _refresh_mr_status(self, project: str, iid: int, attempts: int = 4) -> dict[str, Any] | None:
        import time as _time

        data: dict[str, Any] | None = None
        for _ in range(attempts):
            r = self._get(f"/projects/{self._enc(project)}/merge_requests/{iid}")
            if r is None or r.status_code != 200:
                return None
            data = r.json()
            if data.get("merge_status") != "checking":
                break
            _time.sleep(0.8)
        return data

    def _pack_mr(
        self,
        project: str,
        data: dict[str, Any],
        *,
        source: str,
        target: str,
        files_changed: list[str] | None = None,
        created: bool = False,
    ) -> dict[str, Any]:
        iid = int(data.get("iid") or 0)
        merge_status = data.get("merge_status") or ""
        has_conflicts = bool(data.get("has_conflicts", False))
        mergeable = _mergeable_from_status(merge_status, has_conflicts)
        files = files_changed if files_changed is not None else self._mr_changes(project, iid)
        commits_count = int(data.get("changes_count") or 0) if data.get("changes_count") else len(files)
        web = data.get("web_url") or merge_request_url_for_project(project, iid)
        return {
            "iid": iid,
            "project": project,
            "title": data.get("title", ""),
            "source_branch": data.get("source_branch") or source,
            "target_branch": data.get("target_branch") or target,
            "state": data.get("state", "opened"),
            "merge_status": merge_status,
            "mergeable": mergeable,
            "has_conflicts": has_conflicts,
            "conflicts": files if has_conflicts else [],
            "detail": merge_status or data.get("detailed_merge_status", ""),
            "web_url": web,
            "files_changed": files,
            "commits_count": commits_count,
            "changes_summary": summarize_changes(
                source_branch=source,
                target_branch=target,
                files_changed=files,
                commits_count=commits_count,
                has_conflicts=has_conflicts,
                preview=not created,
            ),
            "created": created,
        }

    def compare(self, project: str, source_ref: str, target_ref: str) -> dict[str, Any]:
        if not source_ref or not target_ref:
            return {"mergeable": None, "ahead": 0, "behind": 0, "conflicts": [],
                    "reason": "Missing source or target branch"}
        det = self._compare_details(project, source_ref, target_ref)
        return {
            "mergeable": None,
            "ahead": det["commits_count"],
            "behind": 0,
            "conflicts": [],
            "files_changed": det["files_changed"],
            "reason": det.get("detail") or "",
        }

    def preview_merge(self, project: str, source: str, target: str) -> dict[str, Any]:
        src_ok = self.branch_exists(project, source) if source else False
        tgt_ok = self.branch_exists(project, target) if target else False
        if not src_ok or not tgt_ok:
            missing = [b for b, ok in ((source, src_ok), (target, tgt_ok)) if b and not ok]
            return {
                "iid": 0,
                "project": project,
                "source_branch": source,
                "target_branch": target,
                "state": "preview",
                "mergeable": False,
                "has_conflicts": False,
                "conflicts": [],
                "detail": f"Branch not found: {', '.join(missing)}",
                "web_url": compare_url_from_project(project, source, target),
                "files_changed": [],
                "commits_count": 0,
                "changes_summary": f"Cannot merge — missing branch(es): {', '.join(missing)}.",
                "created": False,
                "source_exists": src_ok,
                "target_exists": tgt_ok,
            }

        existing = self._find_open_mr(project, source, target)
        if existing:
            refreshed = self._refresh_mr_status(project, int(existing["iid"])) or existing
            packed = self._pack_mr(project, refreshed, source=source, target=target, created=False)
            packed["changes_summary"] = summarize_changes(
                source_branch=source,
                target_branch=target,
                files_changed=packed.get("files_changed") or [],
                commits_count=packed.get("commits_count") or 0,
                has_conflicts=bool(packed.get("has_conflicts")),
            )
            if packed.get("has_conflicts"):
                packed["changes_summary"] += "\nResolve conflicts in GitSpace before submitting."
            else:
                packed["changes_summary"] += "\nOpen MR already exists — it will be linked on submit."
            return packed

        det = self._compare_details(project, source, target)
        files = det["files_changed"]
        commits = det["commits_count"]
        return {
            "iid": 0,
            "project": project,
            "source_branch": source,
            "target_branch": target,
            "state": "preview",
            "merge_status": "unchecked",
            "mergeable": True if commits == 0 and not files else None,
            "has_conflicts": False,
            "conflicts": [],
            "detail": "Preview — MR will be created when you submit.",
            "web_url": compare_url_from_project(project, source, target),
            "files_changed": files,
            "commits_count": commits,
            "changes_summary": summarize_changes(
                source_branch=source,
                target_branch=target,
                files_changed=files,
                commits_count=commits,
                has_conflicts=False,
            ),
            "created": False,
            "source_exists": True,
            "target_exists": True,
        }

    def get_merge_request(self, project: str, iid: int) -> dict[str, Any] | None:
        if not iid:
            return None
        data = self._refresh_mr_status(project, iid)
        if not data:
            return None
        return self._pack_mr(
            project,
            data,
            source=data.get("source_branch", ""),
            target=data.get("target_branch", ""),
            created=True,
        )

    def create_merge_request(self, project: str, source: str, target: str, title: str) -> dict[str, Any]:
        if not self.allow_write:
            preview = self.preview_merge(project, source, target)
            preview["detail"] = "MR creation disabled (read-only token); branch existence verified."
            return preview

        existing = self._find_open_mr(project, source, target)
        if existing:
            refreshed = self._refresh_mr_status(project, int(existing["iid"])) or existing
            return self._pack_mr(project, refreshed, source=source, target=target, created=False)

        r = self._post(
            f"/projects/{self._enc(project)}/merge_requests",
            {"source_branch": source, "target_branch": target, "title": title},
        )
        if r is None:
            return {"iid": 0, "state": "error", "project": project, "mergeable": None,
                    "has_conflicts": False, "source_branch": source, "target_branch": target,
                    "detail": "MR create error: network failure"}
        if r.status_code in (200, 201):
            data = self._refresh_mr_status(project, int(r.json().get("iid", 0))) or r.json()
            return self._pack_mr(project, data, source=source, target=target, created=True)
        if r.status_code == 409:
            again = self._find_open_mr(project, source, target)
            if again:
                refreshed = self._refresh_mr_status(project, int(again["iid"])) or again
                return self._pack_mr(project, refreshed, source=source, target=target, created=False)
        try:
            err = r.json()
            msg = err.get("message", r.text[:200])
        except Exception:
            msg = r.text[:200]
        return {"iid": 0, "state": "error", "project": project, "mergeable": None,
                "has_conflicts": False, "source_branch": source, "target_branch": target,
                "detail": f"MR create failed (HTTP {r.status_code}): {msg}"}

    def accept_merge_request(self, project: str, iid: int) -> dict[str, Any]:
        if not self.allow_write:
            return {"ok": False, "detail": "Merge disabled (read-only token)."}
        refreshed = self._refresh_mr_status(project, iid)
        if refreshed and refreshed.get("state") == "merged":
            return {"ok": True, "state": "merged", "iid": iid, "detail": f"MR !{iid} already merged."}
        if refreshed and refreshed.get("has_conflicts"):
            return {"ok": False, "state": "opened", "iid": iid,
                    "detail": f"MR !{iid} has conflicts — resolve in GitSpace first."}
        r = self._put(
            f"/projects/{self._enc(project)}/merge_requests/{iid}/merge",
            {"merge_commit_message": f"Merged via deployment portal (MR !{iid})"},
        )
        if r is None:
            return {"ok": False, "detail": "Merge failed: network error."}
        if r.status_code in (200, 201):
            data = r.json()
            return {"ok": True, "state": data.get("state", "merged"), "iid": iid,
                    "detail": f"MR !{iid} merged successfully."}
        try:
            err = r.json()
            msg = err.get("message", r.text[:200])
        except Exception:
            msg = r.text[:200]
        return {"ok": False, "state": refreshed.get("state", "opened") if refreshed else "unknown",
                "iid": iid, "detail": f"Merge failed (HTTP {r.status_code}): {msg}"}


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
