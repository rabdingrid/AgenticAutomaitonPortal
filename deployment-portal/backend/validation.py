"""
validation.py — the "smart" pre-merge validator.

Takes a deployment request (the same sections payload the form submits) and
produces a structured **Validation Report** that a Dev Lead reads before
approving. For each enabled section it:

    build              -> opens/inspects a merge request (source -> target),
                          reports mergeability + conflicts + the MR URL.
    yaml / db / phrases -> locates the artifact file on the release branch,
                          runs structural + AI content checks, reports the blob URL.

It then asks the AI (Ollama qwen2.5-coder) to write a human briefing with steps
and insights. Everything degrades gracefully: AI down → deterministic fallback;
GitSpace mock today → real client/webhook later (see gitspace.py).

Run it from main.py (on submit / on demand) or from scripts/run_validation.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import yaml

import ai_client
import catalog
import gitspace
import liquibase_validator


class _DuplicateKeyError(Exception):
    """Raised when a YAML mapping has the same key twice at the same level."""


class _DupCheckLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys *within the same mapping*.

    Unlike a naive line scan, this respects structure: the same key under
    different parents (e.g. `provider:` in two services) is perfectly valid and
    is NOT flagged."""


def _construct_mapping(loader: _DupCheckLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    mapping: dict = {}
    dups: list[str] = []
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            dups.append(str(key))
        mapping[key] = loader.construct_object(value_node, deep=deep)
    if dups:
        raise _DuplicateKeyError(", ".join(dict.fromkeys(dups)))
    return mapping


_DupCheckLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _yaml_checks(content: str) -> list[dict[str, str]]:
    """Validate YAML syntax and detect real (same-mapping) duplicate keys."""
    try:
        # Force full construction so nested mappings are checked too.
        list(yaml.load_all(content, Loader=_DupCheckLoader))
    except _DuplicateKeyError as e:
        return [{"name": "Duplicate keys", "status": "fail",
                 "detail": f"Duplicate key `{e}` within the same block."}]
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark else ""
        return [{"name": "YAML syntax", "status": "fail",
                 "detail": f"Invalid YAML{where}: {getattr(e, 'problem', str(e))}"}]
    return [{"name": "YAML structure", "status": "pass",
             "detail": "Valid YAML; no duplicate keys within any block."}]

_SECTION_TITLES = {
    "build": "Build / Gitspace merge",
    "yaml": "YAML / Config",
    "db": "DB / Liquibase",
    "phrases": "Phrases",
}

_RANK = {"pass": 0, "warn": 1, "fail": 2}
_RANK_INV = {0: "pass", 1: "warn", 2: "fail"}

_ARTIFACT_LABEL = {"yaml": "YAML", "db": "DB/SQL", "phrases": "Phrases"}


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

    if section == "phrases":
        try:
            json.loads(content)
            checks.append({"name": "Valid JSON", "status": "pass", "detail": "Phrases file parses as JSON."})
        except json.JSONDecodeError as e:
            checks.append({"name": "Valid JSON", "status": "fail", "detail": f"JSON parse error: {e}"})
    elif section == "yaml":
        checks.extend(_yaml_checks(content))
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
    )

    if use_ai and lb_checks and lb_checks[0].get("status") == "fail" and meta.get("syntax_errors"):
        contexts = liquibase_validator.build_ai_syntax_contexts(
            file_parts,
            [liquibase_validator.DbFinding(**e) for e in meta["syntax_errors"]],
        )
        hints = ai_client.explain_db_syntax_findings(label, contexts)
        if hints:
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


def _flatten_yaml_keys(data: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested YAML to dotted keys for simple diff."""
    out: dict[str, Any] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                out.update(_flatten_yaml_keys(v, key))
            else:
                out[key] = v
    return out


def _deterministic_new_vs_old_checks(
    section: str, old_content: str, new_content: str, baseline_release: str,
) -> list[dict[str, str]]:
    """Fast diff checks before AI — catches obvious collisions."""
    checks: list[dict[str, str]] = []
    if old_content.strip() == new_content.strip():
        checks.append({
            "name": "New vs old diff",
            "status": "pass",
            "detail": f"Identical to baseline `{baseline_release}` — no changes.",
        })
        return checks

    if section == "yaml":
        try:
            old_data = yaml.safe_load(old_content) or {}
            new_data = yaml.safe_load(new_content) or {}
            old_flat = _flatten_yaml_keys(old_data)
            new_flat = _flatten_yaml_keys(new_data)
            removed = [k for k in old_flat if k not in new_flat]
            changed = [k for k in new_flat if k in old_flat and old_flat[k] != new_flat[k]]
            if removed:
                checks.append({
                    "name": "YAML keys removed",
                    "status": "warn",
                    "detail": f"Keys in baseline but missing in new: {', '.join(removed[:5])}"
                               + ("…" if len(removed) > 5 else "") + ".",
                })
            if changed:
                preview = "; ".join(f"`{k}`: {old_flat[k]!r} → {new_flat[k]!r}" for k in changed[:3])
                checks.append({
                    "name": "YAML value changes",
                    "status": "warn" if len(changed) > 2 else "pass",
                    "detail": f"{len(changed)} value change(s) vs baseline: {preview}"
                               + ("…" if len(changed) > 3 else "") + ".",
                })
            if not removed and not changed:
                checks.append({
                    "name": "YAML structure diff",
                    "status": "pass",
                    "detail": "Parsed YAML differs only in formatting or ordering — no key/value drift.",
                })
        except yaml.YAMLError as e:
            checks.append({
                "name": "YAML diff",
                "status": "warn",
                "detail": f"Could not parse for diff: {e}",
            })

    if not checks:
        checks.append({
            "name": "New vs old diff",
            "status": "pass",
            "detail": f"Content changed vs baseline `{baseline_release}` — see AI comparison.",
        })
    return checks


def _validate_file_item(section: str, link: dict[str, Any], release: str, cfg: dict[str, Any],
                        client: gitspace.GitSpaceClient, use_ai: bool,
                        environment: str = "") -> dict[str, Any]:
    if section == "db":
        return _validate_db_item(link, release, environment, cfg, client, use_ai)

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
    branch_ok = False
    urls: dict[str, str] = {"file": url or ""}
    baseline_info: dict[str, str] | None = None
    if not release:
        checks.append({"name": "Release branch", "status": "fail",
                       "detail": "Release branch is required for this section."})
    elif not client.branch_exists(project, release):
        checks.append({"name": "Branch exists", "status": "fail",
                       "detail": f"Branch `{release}` was not found in `{project}`."})
    else:
        branch_ok = True
        checks.append({"name": "Branch exists", "status": "pass",
                       "detail": f"Branch `{release}` found in `{project}`."})
        section_dir = gitspace.section_dir_for(section, service, release, cfg) or ""
        files = gitspace.resolve_section_files(section, service, release, cfg, client)
        artifact = _ARTIFACT_LABEL.get(section, section.upper())
        if not files:
            checks.append({
                "name": f"{artifact} on release",
                "status": "fail",
                "detail": (
                    f"{label} is not included on release `{release}` — no {artifact} files "
                    f"found under `{section_dir}`. Check the release branch or remove this "
                    f"service from the request."
                ),
            })
        else:
            multi = len(files) > 1
            for fp in files:
                fname = fp.rsplit("/", 1)[-1]
                prefix = f"{fname}: " if multi else ""
                ran_ai_compare = False
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

                # New vs old comparison (YAML only) when a baseline release exists.
                if section == "yaml" and branch_ok:
                    baseline_release = gitspace.resolve_baseline_release(
                        client, project, release, service, release, cfg,
                    )
                    if baseline_release:
                        baseline_path = gitspace.swap_release_in_path(fp, release, baseline_release)
                        old_content = client.get_file(
                            project, baseline_release, baseline_path, artifact_role="old",
                        )
                        baseline_info = {
                            "release": baseline_release,
                            "path": baseline_path,
                        }
                        bl_url = gitspace.baseline_blob_url(
                            section, service, baseline_release, fp, release, cfg,
                        )
                        if bl_url:
                            urls["baseline_file"] = bl_url
                        if old_content is None:
                            checks.append({
                                "name": f"{prefix}Baseline artifact",
                                "status": "warn",
                                "detail": (
                                    f"No file at `{baseline_path}` on `{baseline_release}` — "
                                    "skipping new-vs-old comparison (first release for this service?)."
                                ),
                            })
                        else:
                            det_checks = _deterministic_new_vs_old_checks(
                                section, old_content, content, baseline_release,
                            )
                            for c in det_checks:
                                checks.append({**c, "name": f"{prefix}{c['name']}"})
                            identical = any(
                                "Identical to baseline" in c.get("detail", "")
                                for c in det_checks
                            )
                            ran_ai_compare = False
                            if use_ai and not identical:
                                diff = ai_client.compare_artifacts(
                                    section, label, fp, old_content, content,
                                    baseline_release=baseline_release,
                                    current_release=release,
                                )
                                ran_ai_compare = diff.get("available", False)
                                if diff.get("available"):
                                    ai_ran = True
                                    detail = diff["summary"] or "AI compared new vs baseline."
                                    if diff.get("issues"):
                                        detail += " " + "; ".join(diff["issues"][:3])
                                    checks.append({
                                        "name": f"{prefix}AI new vs old",
                                        "status": diff["verdict"],
                                        "detail": detail,
                                    })
                                    if ai_insight is None:
                                        ai_insight = {
                                            "summary": diff["summary"],
                                            "issues": diff["issues"],
                                            "verdict": diff["verdict"],
                                            "confidence": diff["confidence"],
                                            "baseline_release": baseline_release,
                                            "has_conflicts": diff.get("has_conflicts", False),
                                        }
                                else:
                                    checks.append({
                                        "name": f"{prefix}AI new vs old",
                                        "status": "warn",
                                        "detail": "AI diff unavailable — deterministic checks only.",
                                    })
                            elif identical:
                                ran_ai_compare = True  # skip redundant content review

                skip_review = (
                    section == "yaml"
                    and ran_ai_compare
                    and ai_client.skip_redundant_content_review()
                )
                if use_ai and section in ("yaml", "phrases") and not skip_review:
                    ai = ai_client.analyze_artifact(section, f"{label}/{fname}", fp, content)
                    if ai.get("available"):
                        ai_ran = True
                        if ai_insight is None:
                            ai_insight = {"summary": ai["summary"], "issues": ai["issues"],
                                          "verdict": ai["verdict"], "confidence": ai["confidence"]}
                        checks.append({"name": f"{prefix}AI content review", "status": ai["verdict"],
                                       "detail": ai["summary"] or "AI reviewed the artifact."})

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
        "baseline": baseline_info,
        "ai_ran": ai_ran,
    }


def _validate_build_item(link: dict[str, Any], from_ref: str, to_ref: str, cfg: dict[str, Any],
                         client: gitspace.GitSpaceClient) -> dict[str, Any]:
    service = catalog.get_service(link.get("service_key", "")) or {
        "key": link.get("service_key", ""), "label": link.get("label") or link.get("service_key", ""),
        "type": link.get("sub_type", ""),
    }
    label = service.get("label") or link.get("label") or link.get("service_key", "")
    project = gitspace.service_project_path(service, cfg)

    checks: list[dict[str, str]] = []
    if not from_ref or not to_ref:
        checks.append({"name": "Branch pair", "status": "fail",
                       "detail": "Both source and destination branches are required."})

    mr = client.create_merge_request(project, from_ref, to_ref, f"Deploy {label}: {from_ref} → {to_ref}")
    mergeable = mr.get("mergeable")
    if mergeable is True:
        checks.append({"name": "Mergeability", "status": "pass",
                       "detail": f"Clean merge {from_ref} → {to_ref} (MR !{mr['iid']})."})
    elif mergeable is False:
        conflicts = ", ".join(mr.get("conflicts", [])) or mr.get("detail", "unknown")
        checks.append({"name": "Mergeability", "status": "fail",
                       "detail": f"Cannot merge: {conflicts}."})
    else:
        # Read-only mode (no write scope): MR isn't created. Report branch existence
        # so the section is still useful; mergeability comes once write is enabled.
        src_ok = mr.get("source_exists")
        tgt_ok = mr.get("target_exists")
        if from_ref and to_ref and src_ok and tgt_ok:
            checks.append({"name": "Branches exist", "status": "pass",
                           "detail": f"Both `{from_ref}` and `{to_ref}` exist. "
                                     "Merge request creation deferred (read-only mode)."})
        else:
            missing = [b for b, ok in ((from_ref, src_ok), (to_ref, tgt_ok)) if b and not ok]
            if missing:
                detail = (
                    f"Branch not found in `{project}`: "
                    + ", ".join(f"`{b}`" for b in missing)
                )
            else:
                detail = mr.get("detail", "Branch check unavailable.")
            checks.append({"name": "Branches exist", "status": "fail", "detail": detail})

    status = _worst([c["status"] for c in checks])
    return {
        "service_key": service.get("key", ""),
        "label": label,
        "sub_type": link.get("sub_type", service.get("type", "")),
        "status": status,
        "checks": checks,
        "urls": {
            # Only link a real MR; otherwise point at the branch comparison.
            **({"merge_request": gitspace.merge_request_url(service, mr["iid"], cfg)} if mr.get("iid") else {}),
            "compare": gitspace.compare_url(service, from_ref, to_ref, cfg),
        },
        "merge": {
            "iid": mr["iid"], "state": mr["state"], "mergeable": mergeable,
            "conflicts": mr.get("conflicts", []), "detail": mr.get("detail", ""),
            "source_branch": from_ref, "target_branch": to_ref,
        },
        "ai_insight": None,
    }


def run_validation(
    environment: str,
    jira_id: str,
    sections: list[dict[str, Any]],
    *,
    use_ai: bool = True,
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
            for link in sec.get("links", []):
                if not link.get("service_key"):
                    continue
                bucket["items"].append(_validate_build_item(
                    link, (sec.get("branch_from") or "").strip(), (sec.get("branch_to") or "").strip(), cfg, client))
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
