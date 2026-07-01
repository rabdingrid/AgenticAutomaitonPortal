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

import ai_client
import catalog
import gitspace

_SECTION_TITLES = {
    "build": "Build / Gitspace merge",
    "yaml": "YAML / Config",
    "db": "DB / Liquibase",
    "phrases": "Phrases",
}

_RANK = {"pass": 0, "warn": 1, "fail": 2}
_RANK_INV = {0: "pass", 1: "warn", 2: "fail"}


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
                       "detail": "Expected artifact was not found at the constructed path."})
        return checks
    checks.append({"name": "File present", "status": "pass", "detail": "Artifact located on the release branch."})

    if section == "phrases":
        try:
            json.loads(content)
            checks.append({"name": "Valid JSON", "status": "pass", "detail": "Phrases file parses as JSON."})
        except json.JSONDecodeError as e:
            checks.append({"name": "Valid JSON", "status": "fail", "detail": f"JSON parse error: {e}"})
    elif section == "db":
        low = content.lower()
        if "changeset" not in low:
            checks.append({"name": "Liquibase changeset", "status": "warn",
                           "detail": "No `--changeset author:id` header found."})
        else:
            checks.append({"name": "Liquibase changeset", "status": "pass", "detail": "Changeset header present."})
        if any(op in low for op in ("drop table", "truncate", "delete from")):
            checks.append({"name": "Destructive SQL", "status": "warn",
                           "detail": "Contains DROP/TRUNCATE/DELETE — confirm this is intended."})
    elif section == "yaml":
        # Naive duplicate-key probe at the same indent level.
        seen: dict[str, int] = {}
        dup = None
        for line in content.splitlines():
            if ":" in line and not line.strip().startswith("#"):
                indent = len(line) - len(line.lstrip())
                key = f"{indent}:{line.split(':', 1)[0].strip()}"
                seen[key] = seen.get(key, 0) + 1
                if seen[key] > 1:
                    dup = line.split(":", 1)[0].strip()
        if dup:
            checks.append({"name": "Duplicate keys", "status": "fail", "detail": f"Duplicate key `{dup}` detected."})
        else:
            checks.append({"name": "YAML structure", "status": "pass", "detail": "No duplicate keys detected."})
    return checks


def _validate_file_item(section: str, link: dict[str, Any], release: str, cfg: dict[str, Any],
                        client: gitspace.GitSpaceClient, use_ai: bool) -> dict[str, Any]:
    service = catalog.get_service(link.get("service_key", "")) or {
        "key": link.get("service_key", ""), "label": link.get("label") or link.get("service_key", ""),
        "type": link.get("sub_type", ""),
    }
    label = service.get("label") or link.get("label") or link.get("service_key", "")
    project = (gitspace._repo_for_section(section, gitspace.service_project_path(service, cfg), cfg))
    file_path = gitspace.file_path_for(section, service, release, cfg) or ""
    url = gitspace.blob_url(section, service, release, cfg)

    checks: list[dict[str, str]] = []
    if not release:
        checks.append({"name": "Release branch", "status": "fail",
                       "detail": "Release branch is required for this section."})
    content = client.get_file(project, release or "main", file_path) if file_path else None
    checks.extend(_structural_checks(section, content))

    ai_insight = None
    if use_ai:
        ai = ai_client.analyze_artifact(section, label, file_path, content)
        if ai.get("available"):
            ai_insight = {"summary": ai["summary"], "issues": ai["issues"],
                          "verdict": ai["verdict"], "confidence": ai["confidence"]}
            checks.append({"name": "AI content review", "status": ai["verdict"],
                           "detail": ai["summary"] or "AI reviewed the artifact."})

    status = _worst([c["status"] for c in checks])
    return {
        "service_key": service.get("key", ""),
        "label": label,
        "sub_type": link.get("sub_type", service.get("type", "")),
        "status": status,
        "checks": checks,
        "urls": {"file": url},
        "merge": None,
        "ai_insight": ai_insight,
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
    else:
        conflicts = ", ".join(mr.get("conflicts", [])) or mr.get("detail", "unknown")
        checks.append({"name": "Mergeability", "status": "fail",
                       "detail": f"Cannot merge: {conflicts}."})

    status = _worst([c["status"] for c in checks])
    return {
        "service_key": service.get("key", ""),
        "label": label,
        "sub_type": link.get("sub_type", service.get("type", "")),
        "status": status,
        "checks": checks,
        "urls": {
            "merge_request": gitspace.merge_request_url(service, mr["iid"], cfg),
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
                bucket["items"].append(_validate_file_item(section, link, release, cfg, client, use_ai))

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
        "ai_model": ai_client.MODEL,
        "summary_markdown": "",
    }

    # AI briefing over the structured facts (best-effort).
    facts = {k: report[k] for k in ("environment", "jira_id", "overall_status", "stats", "sections", "steps")}
    if use_ai:
        s = ai_client.summarize_report(facts)
        if s.get("available") and s.get("markdown"):
            report["summary_markdown"] = s["markdown"]
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
