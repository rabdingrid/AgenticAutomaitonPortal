"""
validation.py — the "smart" pre-merge validator.

Takes a deployment request (the same sections payload the form submits) and
produces a structured **Validation Report** that a Dev Lead reads before
approving. For each enabled section it:

    build              -> opens/inspects a merge request (source -> target),
                          reports mergeability + conflicts + the MR URL.
    yaml / db / phrases -> locates the artifact file on the release branch,
                          runs structural checks (YAML syntax/indentation, etc.),
                          reports the blob URL.

It then asks the AI (Ollama qwen2.5-coder) to write a human briefing with steps
and insights. Everything degrades gracefully: AI down → deterministic fallback;
GitSpace mock today → real client/webhook later (see gitspace.py).

Run it from main.py (on submit / on demand) or from scripts/run_validation.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import ai_client
import catalog
import gitspace
from orchestrator import jenkins_params
from validators import liquibase_validator, yaml_validator


def _yaml_checks(
    content: str,
    environment: str = "",
    *,
    issue_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Full YAML syntax, formatting, and indentation validation."""
    passed, rows = yaml_validator.validate_yaml_issues(content, environment=environment)
    if issue_rows is not None:
        issue_rows.extend(rows)
    if passed:
        return [{"name": "YAML validation", "status": "pass", "detail": "Validation Passed"}]
    checks: list[dict[str, str]] = []
    for row in rows:
        checks.append({
            "name": "YAML validation",
            "status": "fail",
            "detail": yaml_validator.format_yaml_issue(row),
        })
    return checks


def _apply_yaml_ai_review(
    label: str,
    filename: str,
    content: str,
    environment: str,
    issue_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, bool]:
    """Merge Ollama remediation hints into deterministic YAML issue rows."""
    if not issue_rows or not ai_client.yaml_review_enabled():
        return issue_rows, None, False
    review = ai_client.review_yaml_file(
        label,
        filename,
        content,
        environment=environment,
        deterministic_issues=issue_rows,
        numbered_snippet=yaml_validator.numbered_snippet(content),
    )
    if not review.get("available"):
        return issue_rows, None, False
    merged = ai_client.merge_yaml_hints(issue_rows, review.get("hints") or [])
    insight: dict[str, Any] = {
        "summary": review.get("summary") or "AI remediation hints for YAML issues.",
        "issues": [
            f"Line {h.get('line')} ({h.get('block') or 'YAML'}): {h.get('fix')}"
            for h in (review.get("hints") or [])
            if h.get("fix")
        ],
        "verdict": "info",
        "confidence": review.get("confidence", 0),
        "filename": filename,
    }
    return merged, insight, True


def _json_checks(content: str) -> list[dict[str, str]]:
    """JSON syntax only — no schema or key validation."""
    try:
        json.loads(content)
        return [{"name": "JSON format validation", "status": "pass", "detail": "Validation Passed"}]
    except json.JSONDecodeError as exc:
        line = exc.lineno or 1
        col = exc.colno or 0
        where = f" (column {col})" if col else ""
        return [{
            "name": "JSON format validation",
            "status": "fail",
            "detail": f"Line {line}{where}: Invalid JSON syntax — {exc.msg}.",
        }]

_SECTION_TITLES = {
    "build": "Build / Gitspace merge",
    "yaml": "YAML / Config",
    "db": "DB / Liquibase",
    "phrases": "Json & SchemaForms",
}

_RANK = {"pass": 0, "warn": 1, "fail": 2}
_RANK_INV = {0: "pass", 1: "warn", 2: "fail"}

_ARTIFACT_LABEL = {"yaml": "YAML", "db": "DB/SQL", "phrases": "Phrases"}

_PHRASES_ARTIFACT_LABEL = {
    "phrases": "Phrases",
    "schemaforms": "SchemaForms",
    "newschemaforms": "NewSchemaForms",
}


def _phrases_service(link: dict[str, Any], service: dict[str, Any]) -> dict[str, Any]:
    """Phrases links use portal repo paths; sub_type selects the folder kind."""
    out = dict(service)
    out["type"] = "portal"
    out["phrases_kind"] = link.get("sub_type") or out.get("phrases_kind") or "phrases"
    return out


def _phrases_artifact_label(phrases_kind: str) -> str:
    return _PHRASES_ARTIFACT_LABEL.get((phrases_kind or "phrases").lower(), "Phrases")


def _missing_artifact_detail(
    section: str, label: str, release: str, section_dir: str,
) -> str:
    """Human-readable 'no files found' message with the correct file type per section."""
    file_types = {
        "yaml": "`.yml`/`.yaml` files",
        "db": "`.sql` files",
        "phrases": "`.json` files",
    }
    files_desc = file_types.get(section, "artifact files")
    return (
        f"{label} is not included on release `{release}` — no {files_desc} "
        f"found under `{section_dir}`. Check the release branch or remove this "
        f"service from the request."
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _worst(statuses: list[str]) -> str:
    if not statuses:
        return "pass"
    return _RANK_INV[max(_RANK.get(s, 1) for s in statuses)]


def _structural_checks(section: str, content: str | None) -> list[dict[str, str]]:
    """Cheap deterministic checks that run even when the AI is offline."""
    checks: list[dict[str, str]] = []
    if content is None:
        checks.append({"name": "File present", "status": "fail",
                       "detail": "Artifact file could not be read from GitSpace."})
        return checks
    checks.append({"name": "File present", "status": "pass", "detail": "Artifact located on the release branch."})

    return checks


def _blob_url_for_path(
    project: str, release: str, path: str, cfg: dict[str, Any],
) -> str:
    return cfg["templates"]["blob"].format(
        base=cfg["base_url"], project=project, ref=release, path=path,
    )


def _validate_db_item(
    link: dict[str, Any],
    release: str,
    environment: str,
    cfg: dict[str, Any],
    client: gitspace.GitSpaceClient,
    use_ai: bool,
) -> dict[str, Any]:
    """Env-aware Liquibase pre-check — no baseline comparison."""
    service = catalog.get_service(link.get("service_key", "")) or {
        "key": link.get("service_key", ""), "label": link.get("label") or link.get("service_key", ""),
        "type": link.get("sub_type", ""),
    }
    label = service.get("label") or link.get("label") or link.get("service_key", "")
    project = gitspace._repo_for_section("db", gitspace.service_project_path(service, cfg), cfg)

    checks: list[dict[str, str]] = []
    ai_insight: dict[str, Any] | None = None
    ai_ran = False
    urls: dict[str, str] = {}
    section_dir = gitspace.section_dir_for("db", service, release, cfg) or ""

    def _fail(detail: str) -> dict[str, Any]:
        checks.append({"name": "DB validation", "status": "fail", "detail": detail})
        return _db_item_result(service, link, label, checks, urls, ai_insight, ai_ran)

    if not release:
        return _fail("Release branch is required for DB validation.")

    if not client.branch_exists(project, release):
        return _fail(f"Branch `{release}` was not found in `{project}`.")

    file_paths = gitspace.resolve_db_files(environment, service, release, cfg, client)
    if not file_paths:
        return _fail(f"No DB SQL files found under `{section_dir}` on `{release}`.")

    found_names = {p.rsplit("/", 1)[-1].lower() for p in file_paths}
    if "common.sql" not in found_names:
        return _fail(f"`Common.sql` is required but was not found under `{section_dir}`.")

    file_parts: list[tuple[str, str]] = []
    for fp in file_paths:
        fname = fp.rsplit("/", 1)[-1]
        content = client.get_file(project, release, fp)
        if content is None:
            return _fail(f"Could not read `{fname}` on `{release}`.")
        file_parts.append((fname, content))
        urls[fname] = _blob_url_for_path(project, release, fp, cfg)

    merged = liquibase_validator.merge_db_contents(file_parts)
    validated_names = [fname for fname, _ in file_parts]
    lb_checks, meta = liquibase_validator.validate_merged_changelog(
        merged,
        validated_files=validated_names,
        environment=environment,
        file_parts=file_parts,
        defer_sql_syntax=use_ai and ai_client.db_sql_review_enabled(),
    )

    if use_ai and ai_client.db_sql_review_enabled():
        review_ctx = liquibase_validator.build_ai_sql_review_contexts(file_parts, meta)
        errors = [liquibase_validator.DbFinding(**e) for e in meta.get("syntax_errors", [])]
        warnings = [liquibase_validator.DbFinding(**w) for w in meta.get("syntax_warnings", [])]
        # Fast local pass: sqlglot/heuristics find EVERY error (with correct line numbers).
        # Ollama only polishes message/hint — it must never shrink or replace that list.
        flagged_ctx = [
            c for c in review_ctx
            if liquibase_validator.changeset_has_syntax_issues(file_parts, c["changeset"])
        ]
        flagged_keys = {c["changeset"] for c in flagged_ctx}

        if flagged_keys:
            lb_checks, meta = liquibase_validator.append_deterministic_sql_syntax_for_changesets(
                file_parts, errors, warnings, meta, flagged_keys,
            )
            errors = [
                liquibase_validator.DbFinding(**e)
                for e in meta.get("syntax_errors", [])
            ]
            review_result = ai_client.review_db_sql_syntax(
                label, flagged_ctx, environment=environment,
            )
            ai_findings = dict(review_result.findings) if review_result else {}
            if ai_findings:
                lb_checks, meta = liquibase_validator.merge_ai_sql_hints_into_findings(
                    errors, warnings, meta, ai_findings,
                )
        else:
            lb_checks, meta = liquibase_validator.apply_ai_sql_review_findings(
                errors, warnings, meta, {}, set(),
            )
        ai_ran = True
    elif (
        use_ai
        and lb_checks
        and lb_checks[0].get("status") == "fail"
        and meta.get("syntax_errors")
    ):
        errors = [liquibase_validator.DbFinding(**e) for e in meta["syntax_errors"]]
        contexts = liquibase_validator.build_ai_syntax_contexts(file_parts, errors)
        hints = ai_client.explain_db_syntax_findings(label, contexts) or {}
        lb_checks = liquibase_validator.rebuild_db_checks_with_hints(meta, hints)
        ai_ran = True

    checks.extend(lb_checks)

    if use_ai and merged.strip() and not ai_ran:
        review = ai_client.review_db_changelog(label, environment, validated_names, merged)
        if review.get("available"):
            ai_ran = True
            # Only surface AI when it flags a real problem not already covered.
            actionable = [
                i for i in (review.get("issues") or [])
                if i and "pending" not in i.lower()
            ]
            ai_insight = {
                "summary": review["summary"],
                "issues": actionable,
                "verdict": "info",
                "confidence": review["confidence"],
                "validated_files": validated_names,
                "approved_changesets": meta.get("changeset_ids", []),
            }

    return _db_item_result(service, link, label, checks, urls, ai_insight, ai_ran)


def _db_item_result(
    service: dict[str, Any],
    link: dict[str, Any],
    label: str,
    checks: list[dict[str, str]],
    urls: dict[str, str],
    ai_insight: dict[str, Any] | None,
    ai_ran: bool,
) -> dict[str, Any]:
    status = _worst([c["status"] for c in checks])
    return {
        "service_key": service.get("key", ""),
        "label": label,
        "sub_type": link.get("sub_type", service.get("type", "")),
        "status": status,
        "checks": checks,
        "urls": urls,
        "merge": None,
        "ai_insight": ai_insight,
        "baseline": None,
        "ai_ran": ai_ran,
    }


def _validate_phrases_item(
    link: dict[str, Any],
    release: str,
    cfg: dict[str, Any],
    client: gitspace.GitSpaceClient,
) -> dict[str, Any]:
    """Compact JSON validation — pass shows file count; fail shows reason only."""
    service = catalog.get_service(link.get("service_key", "")) or {
        "key": link.get("service_key", ""), "label": link.get("label") or link.get("service_key", ""),
        "type": link.get("sub_type", ""),
    }
    service = _phrases_service(link, service)
    label = service.get("label") or link.get("label") or link.get("service_key", "")
    project = gitspace._repo_for_section("phrases", gitspace.service_project_path(service, cfg), cfg)
    urls: dict[str, str] = {}
    check_name = "JSON validation"

    def _result(checks: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "service_key": service.get("key", ""),
            "label": label,
            "sub_type": link.get("sub_type", service.get("phrases_kind") or service.get("type", "")),
            "status": _worst([c["status"] for c in checks]),
            "checks": checks,
            "urls": urls,
            "merge": None,
            "yaml_files": None,
            "ai_insight": None,
            "baseline": None,
            "ai_ran": False,
        }

    if not release:
        return _result([{"name": check_name, "status": "fail",
                         "detail": "Release branch is required."}])

    if not client.branch_exists(project, release):
        return _result([{"name": check_name, "status": "fail",
                         "detail": f"Branch `{release}` was not found in `{project}`."}])

    section_dir = gitspace.section_dir_for("phrases", service, release, cfg) or ""
    folder_url = gitspace.tree_url(project, release, section_dir, cfg)
    if folder_url:
        urls["folder"] = folder_url

    files = gitspace.resolve_section_files("phrases", service, release, cfg, client)
    if not files:
        return _result([{"name": check_name, "status": "fail",
                         "detail": f"No JSON files found under `{section_dir}`."}])

    failures: list[str] = []
    json_files_meta: list[dict[str, str]] = []
    phrases_kind = (link.get("sub_type") or service.get("phrases_kind") or "phrases").lower()
    for fp in files:
        fname = fp.rsplit("/", 1)[-1]
        if phrases_kind == "phrases":
            json_files_meta.append({
                "path": fp,
                "filename": fname,
                "action": jenkins_params.json_action_for_phrases_filename(fname),
            })
        content = client.get_file(project, release, fp)
        if content is None:
            failures.append(f"Could not read `{fname}`.")
            continue
        for c in _json_checks(content):
            if c["status"] == "fail":
                failures.append(f"{fname}: {c['detail']}")

    if failures:
        return _result([
            {"name": check_name, "status": "fail", "detail": reason}
            for reason in failures
        ])

    n = len(files)
    file_word = "file" if n == 1 else "files"
    result = _result([{
        "name": check_name,
        "status": "pass",
        "detail": f"Found {n} JSON {file_word} — Validation Passed",
    }])
    if phrases_kind == "phrases" and json_files_meta:
        result["json_files"] = jenkins_params.sort_phrases_json_deployments(json_files_meta)
    return result


def _validate_file_item(section: str, link: dict[str, Any], release: str, cfg: dict[str, Any],
                        client: gitspace.GitSpaceClient, use_ai: bool,
                        environment: str = "") -> dict[str, Any]:
    if section == "db":
        return _validate_db_item(link, release, environment, cfg, client, use_ai)
    if section == "phrases":
        return _validate_phrases_item(link, release, cfg, client)

    service = catalog.get_service(link.get("service_key", "")) or {
        "key": link.get("service_key", ""), "label": link.get("label") or link.get("service_key", ""),
        "type": link.get("sub_type", ""),
    }
    label = service.get("label") or link.get("label") or link.get("service_key", "")
    project = (gitspace._repo_for_section(section, gitspace.service_project_path(service, cfg), cfg))
    url = gitspace.blob_url(section, service, release, cfg)

    checks: list[dict[str, str]] = []
    ai_insight: dict[str, Any] | None = None
    ai_ran = False
    urls: dict[str, str] = {}
    yaml_files_meta: list[dict[str, str]] = []
    yaml_ai_insights: list[dict[str, Any]] = []
    if not release:
        checks.append({"name": "Release branch", "status": "fail",
                       "detail": "Release branch is required for this section."})
    elif not client.branch_exists(project, release):
        checks.append({"name": "Branch exists", "status": "fail",
                       "detail": f"Branch `{release}` was not found in `{project}`."})
    else:
        checks.append({"name": "Branch exists", "status": "pass",
                       "detail": f"Branch `{release}` found in `{project}`."})
        section_dir = gitspace.section_dir_for(section, service, release, cfg) or ""
        folder_url = gitspace.tree_url(project, release, section_dir, cfg)
        if folder_url:
            urls["folder"] = folder_url
        files = gitspace.resolve_section_files(section, service, release, cfg, client)
        artifact = _ARTIFACT_LABEL.get(section, section.upper())
        if not files:
            checks.append({
                "name": f"{artifact} on release",
                "status": "fail",
                "detail": _missing_artifact_detail(section, label, release, section_dir),
            })
        else:
            multi = len(files) > 1
            for fp in files:
                fname = fp.rsplit("/", 1)[-1]
                prefix = f"{fname}: " if multi else ""
                if section == "yaml":
                    yaml_files_meta.append({
                        "path": fp,
                        "filename": fname,
                        "action": jenkins_params.yaml_action_for_filename(fname),
                    })
                content = client.get_file(project, release, fp)
                if content is None:
                    checks.append({
                        "name": f"{prefix}File readable",
                        "status": "fail",
                        "detail": f"Could not read `{fp}` on `{release}`.",
                    })
                    continue
                for c in _structural_checks(section, content):
                    checks.append({**c, "name": f"{prefix}{c['name']}"})
                if section == "yaml":
                    file_issue_rows: list[dict[str, Any]] = []
                    yaml_file_checks = _yaml_checks(content, environment, issue_rows=file_issue_rows)
                    if (
                        use_ai
                        and file_issue_rows
                        and content is not None
                    ):
                        merged_rows, file_insight, ran = _apply_yaml_ai_review(
                            label, fname, content, environment, file_issue_rows,
                        )
                        if ran:
                            ai_ran = True
                            if file_insight:
                                yaml_ai_insights.append(file_insight)
                            yaml_file_checks = [
                                {
                                    "name": "YAML validation",
                                    "status": "fail",
                                    "detail": yaml_validator.format_yaml_issue(row),
                                }
                                for row in merged_rows
                            ]
                    for c in yaml_file_checks:
                        checks.append({**c, "name": f"{prefix}{c['name']}"})

    if yaml_ai_insights:
        ai_insight = {
            "summary": yaml_ai_insights[0].get("summary", ""),
            "issues": [i for ins in yaml_ai_insights for i in (ins.get("issues") or [])][:10],
            "verdict": "info",
            "confidence": max(int(ins.get("confidence") or 0) for ins in yaml_ai_insights),
        }

    status = _worst([c["status"] for c in checks])
    return {
        "service_key": service.get("key", ""),
        "label": label,
        "sub_type": link.get("sub_type", service.get("phrases_kind") or service.get("type", "")),
        "status": status,
        "checks": checks,
        "urls": urls,
        "merge": None,
        "yaml_files": yaml_files_meta if section == "yaml" and yaml_files_meta else None,
        "ai_insight": ai_insight,
        "baseline": None,
        "ai_ran": ai_ran,
    }


def _validate_build_item(
    link: dict[str, Any],
    from_ref: str,
    to_ref: str,
    cfg: dict[str, Any],
    client: gitspace.GitSpaceClient,
    build_only: bool = False,
    *,
    create_mrs: bool = False,
) -> dict[str, Any]:
    service = catalog.get_service(link.get("service_key", "")) or {
        "key": link.get("service_key", ""), "label": link.get("label") or link.get("service_key", ""),
        "type": link.get("sub_type", ""),
    }
    label = service.get("label") or link.get("label") or link.get("service_key", "")
    project = gitspace.service_project_path(service, cfg)

    # Build-only: no merge, no branch pair — the Jenkins build runs with a blank
    # MergeID. Nothing to validate against GitSpace, so report a clean pass.
    if build_only:
        return {
            "service_key": service.get("key", ""),
            "label": label,
            "sub_type": link.get("sub_type", service.get("type", "")),
            "status": "pass",
            "checks": [{"name": "Build only", "status": "pass",
                        "detail": "Build-only run — no merge, blank MergeID; Jenkins build will run directly."}],
            "urls": {},
            "merge": None,
            "ai_insight": None,
        }

    checks: list[dict[str, str]] = []
    if not from_ref or not to_ref:
        checks.append({"name": "Branch pair", "status": "fail",
                       "detail": "Both source and destination branches are required."})

    title = f"Deploy {label}: {from_ref} → {to_ref}"
    if create_mrs:
        mr = client.create_merge_request(project, from_ref, to_ref, title)
    else:
        mr = client.preview_merge(project, from_ref, to_ref)

    mergeable = mr.get("mergeable")
    has_conflicts = bool(mr.get("has_conflicts"))
    src_ok = mr.get("source_exists", True)
    tgt_ok = mr.get("target_exists", True)

    if not from_ref or not to_ref:
        pass
    elif not src_ok or not tgt_ok:
        missing = [b for b, ok in ((from_ref, src_ok), (to_ref, tgt_ok)) if b and not ok]
        detail = (
            f"Branch not found in `{project}`: "
            + ", ".join(f"`{b}`" for b in missing)
        )
        checks.append({"name": "Branches exist", "status": "fail", "detail": detail})
    else:
        checks.append({"name": "Branches exist", "status": "pass",
                       "detail": f"Both `{from_ref}` and `{to_ref}` exist in `{project}`."})

    if has_conflicts or mergeable is False:
        conflict_files = mr.get("conflicts") or mr.get("files_changed") or []
        conflict_text = ", ".join(conflict_files[:5]) or mr.get("detail", "merge conflict")
        if len(conflict_files) > 5:
            conflict_text += f" (+{len(conflict_files) - 5} more)"
        checks.append({
            "name": "Merge conflicts",
            "status": "fail",
            "detail": f"Cannot merge {from_ref} → {to_ref}: {conflict_text}. Resolve in GitSpace before submitting.",
        })
    elif mergeable is True:
        if create_mrs and mr.get("iid"):
            checks.append({
                "name": "Merge request",
                "status": "pass",
                "detail": f"MR !{mr['iid']} opened ({from_ref} → {to_ref}) — not merged yet; Dev Lead approval will merge.",
            })
        else:
            checks.append({
                "name": "Merge preview",
                "status": "pass",
                "detail": f"No conflicts — {from_ref} → {to_ref} is ready. MR will be created on submit.",
            })
    elif create_mrs and mr.get("state") == "error":
        checks.append({
            "name": "Merge request",
            "status": "fail",
            "detail": mr.get("detail", "Could not create merge request."),
        })
    elif not create_mrs:
        summary = (mr.get("changes_summary") or "").split("\n")[0]
        checks.append({
            "name": "Merge preview",
            "status": "pass" if summary else "warn",
            "detail": summary or mr.get("detail", "Preview complete — submit to open merge request."),
        })

    mr_url = mr.get("web_url") or (
        gitspace.merge_request_url(service, mr["iid"], cfg) if mr.get("iid") else ""
    )
    compare_url = gitspace.compare_url(service, from_ref, to_ref, cfg)

    status = _worst([c["status"] for c in checks])
    return {
        "service_key": service.get("key", ""),
        "label": label,
        "sub_type": link.get("sub_type", service.get("type", "")),
        "status": status,
        "checks": checks,
        "urls": {
            **({"merge_request": mr_url} if mr_url else {}),
            "compare": compare_url,
            "gitspace": mr_url or compare_url,
        },
        "merge": {
            "iid": mr.get("iid", 0),
            "project": project,
            "state": mr.get("state", "preview" if not create_mrs else "opened"),
            "mergeable": mergeable,
            "has_conflicts": has_conflicts,
            "conflicts": mr.get("conflicts", []),
            "files_changed": mr.get("files_changed", []),
            "commits_count": mr.get("commits_count", 0),
            "changes_summary": mr.get("changes_summary", ""),
            "web_url": mr_url,
            "detail": mr.get("detail", ""),
            "source_branch": from_ref,
            "target_branch": to_ref,
            "created": bool(mr.get("created", create_mrs and bool(mr.get("iid")))),
        },
        "ai_insight": None,
    }


def run_validation(
    environment: str,
    jira_id: str,
    sections: list[dict[str, Any]],
    *,
    use_ai: bool = True,
    create_mrs: bool = False,
) -> dict[str, Any]:
    """Build the full structured report for a request. Pure/​stateless — callers
    persist the result (e.g. onto a task)."""
    cfg = gitspace.load_config()
    client = gitspace.get_client()

    grouped: dict[str, dict[str, Any]] = {}
    for sec in sections:
        section = sec.get("section")
        if section not in _SECTION_TITLES:
            continue
        bucket = grouped.setdefault(section, {"section": section, "title": _SECTION_TITLES[section], "items": []})
        if section == "build":
            build_only = bool(sec.get("build_only"))
            for link in sec.get("links", []):
                if not link.get("service_key"):
                    continue
                bucket["items"].append(_validate_build_item(
                    link, (sec.get("branch_from") or "").strip(), (sec.get("branch_to") or "").strip(),
                    cfg, client, build_only, create_mrs=create_mrs))
        else:
            release = (sec.get("release_branch") or "").strip()
            for link in sec.get("links", []):
                if not link.get("service_key"):
                    continue
                bucket["items"].append(_validate_file_item(
                    section, link, release, cfg, client, use_ai, environment))

    section_results = []
    for section in ("yaml", "db", "phrases", "build"):
        if section in grouped:
            g = grouped[section]
            g["status"] = _worst([it["status"] for it in g["items"]])
            section_results.append(g)

    all_items = [it for g in section_results for it in g["items"]]
    stats = {
        "checked": len(all_items),
        "passed": sum(1 for it in all_items if it["status"] == "pass"),
        "warnings": sum(1 for it in all_items if it["status"] == "warn"),
        "failed": sum(1 for it in all_items if it["status"] == "fail"),
    }
    overall = _worst([it["status"] for it in all_items]) if all_items else "fail"
    overall_label = {"pass": "passed", "warn": "warnings", "fail": "failed"}[overall]

    steps = _recommended_steps(section_results, overall)

    report: dict[str, Any] = {
        "generated_at": _now(),
        "environment": environment,
        "jira_id": jira_id,
        "overall_status": overall_label,
        "stats": stats,
        "sections": section_results,
        "steps": steps,
        "ai_used": False,
        "ai_model": ai_client.active_model(),
        "summary_markdown": "",
    }

    # AI briefing over the structured facts (best-effort). Skippable via VALIDATION_AI_SUMMARY=false.
    facts = {k: report[k] for k in ("environment", "jira_id", "overall_status", "stats", "sections", "steps")}
    any_ai = any(it.get("ai_ran") for g in section_results for it in g["items"])
    if use_ai and ai_client.ai_summary_enabled():
        s = ai_client.summarize_report(facts)
        if s.get("available") and s.get("markdown"):
            report["summary_markdown"] = s["markdown"]
            report["ai_used"] = True
        elif any_ai:
            report["ai_used"] = True
    elif any_ai:
        report["ai_used"] = True
    if not report["summary_markdown"]:
        report["summary_markdown"] = _fallback_markdown(report)

    return report


def _recommended_steps(section_results: list[dict[str, Any]], overall: str) -> list[str]:
    steps: list[str] = []
    for g in section_results:
        for it in g["items"]:
            for c in it["checks"]:
                if c["status"] == "fail":
                    steps.append(f"Fix [{g['title']} · {it['label']}] — {c['name']}: {c['detail']}")
                elif c["status"] == "warn":
                    steps.append(f"Review [{g['title']} · {it['label']}] — {c['name']}: {c['detail']}")
    if overall == "pass":
        steps.append("All checks passed — Dev Lead can approve; orchestrator will proceed on approval.")
    elif overall == "warn":
        steps.append("Resolve the warnings above or acknowledge them, then approve.")
    else:
        steps.append("Resolve the failures above and re-run validation before approving.")
    return steps


def _fallback_markdown(report: dict[str, Any]) -> str:
    verdict = {"passed": "APPROVE-READY", "warnings": "NEEDS ATTENTION", "failed": "BLOCKED"}[report["overall_status"]]
    s = report["stats"]
    lines = [
        f"## Verdict — {verdict}",
        f"{s['passed']} passed · {s['warnings']} warning(s) · {s['failed']} failed across {s['checked']} item(s) "
        f"for **{report['jira_id'] or 'request'}** → **{report['environment']}**.",
        "",
        "## Checks",
    ]
    for g in report["sections"]:
        lines.append(f"**{g['title']}**")
        for it in g["items"]:
            icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}[it["status"]]
            url = it["urls"].get("merge_request") or it["urls"].get("file") or ""
            lines.append(f"- {icon} {it['label']} — " + "; ".join(c["detail"] for c in it["checks"]))
            if url:
                lines.append(f"  - {url}")
    lines += ["", "## Recommended next steps"]
    lines += [f"{i+1}. {st}" for i, st in enumerate(report["steps"])]
    lines.append("")
    lines.append("_AI briefing unavailable — this is the deterministic fallback report._")
    return "\n".join(lines)
