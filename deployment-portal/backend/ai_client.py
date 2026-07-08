"""
ai_client.py — thin Ollama wrapper used by the validator.

Talks to a local Ollama server (default http://localhost:11434) running the
qwen2.5-coder model. Two helpers:

    analyze(...)  -> structured JSON verdict for one artifact (yaml/db/phrases)
    summarize(...) -> markdown report a Dev Lead can read before approving

Everything is best-effort: if Ollama is unreachable or slow, callers get a
clear `available=False` result and the validator falls back to deterministic
output, so a deployment request is never blocked by the AI being down.

Override with env vars: OLLAMA_BASE_URL, OLLAMA_MODEL.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
_PREFERRED_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "120"))
# Generous timeout for listing models — a cold Ollama that is still starting/
# loading can take several seconds to answer /api/tags.
_TAGS_TIMEOUT = float(os.getenv("OLLAMA_TAGS_TIMEOUT", "15"))
# Retry transient failures (cold model load, brief 500s) before giving up and
# falling back to the deterministic report. Keeps AI working "most of the time".
_RETRIES = int(os.getenv("OLLAMA_RETRIES", "2"))
# Keep the model resident so subsequent validations are fast and reliable.
_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")
# Token caps per call type — JSON checks need far fewer tokens than the markdown briefing.
_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "1400"))
_NUM_PREDICT_JSON = int(os.getenv("OLLAMA_NUM_PREDICT_JSON", "384"))
_NUM_PREDICT_SUMMARY = int(os.getenv("OLLAMA_NUM_PREDICT_SUMMARY", "700"))
# Reuse one HTTP client (Ollama is local; avoids TCP setup per call).
_HTTP: httpx.Client | None = None

_resolved_model: str | None = None


def _http_client(timeout: float) -> httpx.Client:
    global _HTTP
    if _HTTP is None:
        _HTTP = httpx.Client(trust_env=False, timeout=timeout)
    return _HTTP


def ai_summary_enabled() -> bool:
    """Full AI markdown briefing at end of validation (slowest call). Disable for speed."""
    return os.getenv("VALIDATION_AI_SUMMARY", "true").lower() in ("1", "true", "yes")


def skip_redundant_content_review() -> bool:
    """When new-vs-old AI compare ran, skip separate content-review call (default on)."""
    return os.getenv("VALIDATION_AI_SKIP_REDUNDANT_REVIEW", "true").lower() in ("1", "true", "yes")


def _client(timeout: float) -> httpx.Client:
    """HTTP client for the LOCAL Ollama server.

    `trust_env=False` makes httpx ignore HTTP(S)_PROXY env vars. This is critical
    on a corporate VPN, which typically exports a proxy that would otherwise try
    to route even localhost:11434 calls through the proxy and fail — making the
    AI look "down" whenever the VPN is connected.
    """
    return httpx.Client(trust_env=False, timeout=timeout)


def _list_models() -> list[str]:
    for _ in range(_RETRIES + 1):
        try:
            with _client(_TAGS_TIMEOUT) as c:
                r = c.get(f"{BASE_URL}/api/tags")
            if r.status_code == 200:
                return [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]
        except Exception:
            continue
    return []


def resolve_model() -> str | None:
    """Pick OLLAMA_MODEL if installed, else the first model Ollama reports."""
    global _resolved_model
    if _resolved_model:
        return _resolved_model
    models = _list_models()
    if not models:
        return None
    if _PREFERRED_MODEL in models:
        _resolved_model = _PREFERRED_MODEL
    else:
        _resolved_model = models[0]
    return _resolved_model


def active_model() -> str:
    return resolve_model() or _PREFERRED_MODEL


def is_available() -> bool:
    return resolve_model() is not None


def _generate(
    prompt: str,
    *,
    system: str | None = None,
    fmt_json: bool = False,
    num_predict: int | None = None,
) -> str | None:
    model = resolve_model()
    if not model:
        return None
    predict = num_predict if num_predict is not None else (_NUM_PREDICT_JSON if fmt_json else _NUM_PREDICT)
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": _KEEP_ALIVE,
        "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": predict},
    }
    if system:
        payload["system"] = system
    if fmt_json:
        payload["format"] = "json"
    client = _http_client(_TIMEOUT)
    for _ in range(_RETRIES + 1):
        try:
            r = client.post(f"{BASE_URL}/api/generate", json=payload)
            r.raise_for_status()
            resp = r.json().get("response", "").strip()
            if resp:
                return resp
        except Exception:
            continue
    return None


def warm_up() -> bool:
    """Load the model into memory so the first real validation is fast and
    reliable. Safe to call in a background thread at startup; never raises."""
    model = resolve_model()
    if not model:
        return False
    try:
        with _client(_TIMEOUT) as c:
            c.post(
                f"{BASE_URL}/api/generate",
                json={"model": model, "prompt": "ready", "stream": False,
                      "keep_alive": _KEEP_ALIVE, "options": {"num_predict": 1}},
            )
        return True
    except Exception:
        return False


_COMPARE_SYSTEM = (
    "You are a release-engineering diff reviewer. You compare an OLD (baseline) artifact "
    "already on a prior release with a NEW artifact being promoted. Identify conflicts, "
    "regressions, duplicate Liquibase changeset IDs, incompatible config changes, and "
    "breaking SQL. Be specific: cite keys, lines, or changeset ids. Respond ONLY with JSON."
)


def compare_artifacts(
    section: str,
    label: str,
    file_path: str,
    old_content: str,
    new_content: str,
    *,
    baseline_release: str,
    current_release: str,
) -> dict[str, Any]:
    """Compare new vs old YAML/DB artifacts. Returns same shape as analyze_artifact plus
    `has_conflicts` and `baseline_release`."""
    empty = {
        "available": False,
        "verdict": "warn",
        "summary": "AI diff unavailable.",
        "issues": [],
        "confidence": 0,
        "has_conflicts": False,
        "baseline_release": baseline_release,
    }
    if not old_content or not new_content:
        return {**empty, "summary": "Cannot compare — old or new artifact missing."}
    if old_content.strip() == new_content.strip():
        return {
            "available": True,
            "verdict": "pass",
            "summary": "New artifact is identical to the baseline — no changes detected.",
            "issues": [],
            "confidence": 100,
            "has_conflicts": False,
            "baseline_release": baseline_release,
        }

    cap = 2200
    old_body = old_content[:cap]
    new_body = new_content[:cap]
    type_label = "YAML config" if section == "yaml" else "Liquibase SQL" if section == "db" else section

    prompt = (
        f"Artifact type: {type_label} ({section})\n"
        f"Service/Portal: {label}\n"
        f"File path (new): {file_path}\n"
        f"Baseline release: {baseline_release}\n"
        f"New release: {current_release}\n\n"
        f"--- OLD (baseline on `{baseline_release}`) ---\n{old_body}\n"
        f"--- END OLD ---\n\n"
        f"--- NEW (on `{current_release}`) ---\n{new_body}\n"
        f"--- END NEW ---\n\n"
        "Compare OLD vs NEW. Flag:\n"
        "- YAML: removed required keys, type changes, port/url changes that break other envs, "
        "duplicate keys, conflicting values for the same key\n"
        "- For YAML: missing/empty S3, AWS, bucket, region, or secret placeholders are "
        "ADVISORY (verdict warn, not fail) — often set at deploy time from SSM/vault.\n"
        "- SQL: duplicate `--changeset author:id` already in OLD, destructive ops added in NEW, "
        "column type narrowing, conflicting ALTER on same column\n"
        "Set has_conflicts=true when the new artifact would break deploy or collide with old.\n"
        "verdict: pass=no issues, warn=review needed, fail=blocking conflict\n"
        'Respond with JSON: {"verdict":"pass|warn|fail","summary":"one sentence",'
        '"has_conflicts":true|false,"issues":["specific conflict or risk",...],"confidence":0-100}'
    )
    raw = _generate(prompt, system=_COMPARE_SYSTEM, fmt_json=True)
    if raw is None:
        return empty
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "available": True,
            "verdict": "warn",
            "summary": raw[:200] if raw else "Unparseable AI diff response.",
            "issues": [],
            "confidence": 0,
            "has_conflicts": False,
            "baseline_release": baseline_release,
        }
    verdict = str(data.get("verdict", "warn")).lower()
    if verdict not in ("pass", "warn", "fail"):
        verdict = "warn"
    issues = data.get("issues") or []
    if isinstance(issues, str):
        issues = [issues]
    issues = [str(i) for i in issues][:10]
    summary = str(data.get("summary", "")).strip()
    if section == "yaml":
        verdict = _cap_yaml_ai_verdict(verdict, summary, issues)
        has_conflicts = verdict == "fail" and bool(data.get("has_conflicts", False))
    else:
        has_conflicts = bool(data.get("has_conflicts", verdict == "fail"))
    return {
        "available": True,
        "verdict": verdict,
        "summary": summary,
        "issues": issues,
        "confidence": int(data.get("confidence", 0) or 0),
        "has_conflicts": has_conflicts,
        "baseline_release": baseline_release,
    }


_DB_REVIEW_SYSTEM = (
    "You are a database migration reviewer. Review Liquibase SQL for migration risks, "
    "dangerous operations, malformed Liquibase usage, and deployment concerns. "
    "Only Approved changesets are deployed; Pending changesets are ignored. "
    "Do NOT assign pass/fail — provide actionable insights only. Respond ONLY with JSON."
)


def review_db_changelog(
    label: str,
    environment: str,
    validated_files: list[str],
    merged_content: str,
) -> dict[str, Any]:
    """Advisory AI review of merged DB changelog — never blocks validation."""
    empty = {
        "available": False,
        "summary": "AI DB review unavailable.",
        "issues": [],
        "confidence": 0,
    }
    if not merged_content or not merged_content.strip():
        return {**empty, "summary": "No SQL content to review."}

    files_line = ", ".join(validated_files) if validated_files else "merged changelog"
    body = merged_content[:4000]
    prompt = (
        f"Service: {label}\n"
        f"Target environment: {environment}\n"
        f"Files merged: {files_line}\n\n"
        f"--- MERGED LIQUIBASE SQL ---\n{body}\n--- END ---\n\n"
        "Review for:\n"
        "- migration risks and locking concerns\n"
        "- dangerous operations (DROP/TRUNCATE/DELETE)\n"
        "- malformed Liquibase headers (must use `-- Changeset author:id` with space after `--`)\n"
        "- missing labels:Approved on deployable changesets\n"
        "- duplicate changeset author:id values\n"
        "- SQL that may fail at deploy time\n\n"
        "Provide insights only — do not output pass/fail verdict.\n"
        'Respond with JSON: {"summary":"one sentence overview",'
        '"issues":["specific risk or recommendation",...],"confidence":0-100}'
    )
    raw = _generate(prompt, system=_DB_REVIEW_SYSTEM, fmt_json=True)
    if raw is None:
        return empty
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "available": True,
            "summary": raw[:200] if raw else "Unparseable AI DB review.",
            "issues": [],
            "confidence": 0,
        }
    issues = data.get("issues") or []
    if isinstance(issues, str):
        issues = [issues]
    return {
        "available": True,
        "summary": str(data.get("summary", "")).strip(),
        "issues": [str(i) for i in issues][:8],
        "confidence": int(data.get("confidence", 0) or 0),
    }


_DB_EXPLAIN_SYSTEM = (
    "You are a Liquibase/MySQL SQL reviewer. Given deterministic syntax errors with "
    "source snippets, write one short fix sentence per changeset. Be specific (mention "
    "line, typo, or missing semicolon). Respond ONLY with JSON."
)


def explain_db_syntax_findings(
    label: str,
    contexts: list[dict[str, Any]],
) -> dict[str, str]:
    """Per-changeset AI fix hints for deterministic SQL errors (advisory)."""
    if not contexts:
        return {}
    blocks: list[str] = []
    for ctx in contexts[:6]:
        lines = [f"Changeset {ctx['changeset']} ({ctx.get('file', '')} @ line {ctx.get('header_line', '')})"]
        for f in ctx.get("findings", [])[:4]:
            lines.append(f"  L{f.get('line')}: {f.get('message')}")
            if f.get("snippet"):
                lines.append(f.get("snippet"))
        blocks.append("\n".join(lines))
    prompt = (
        f"Service: {label}\n\n"
        + "\n\n".join(blocks)
        + "\n\nFor each changeset, give one fix sentence (max 120 chars).\n"
        'Respond JSON: {"hints":{"author:id":"fix sentence",...}}'
    )
    raw = _generate(prompt, system=_DB_EXPLAIN_SYSTEM, fmt_json=True)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        hints = data.get("hints") or data
        if not isinstance(hints, dict):
            return {}
        return {str(k): str(v).strip() for k, v in hints.items() if v}
    except json.JSONDecodeError:
        return {}


_ANALYZE_SYSTEM = (
    "You are a meticulous release-engineering reviewer for a deployment portal. "
    "You inspect a single configuration/migration/phrases artifact and judge whether it is "
    "safe to merge and deploy. Be specific and developer-actionable: name the exact key, line, "
    "or statement that is wrong and how to fix it. Respond ONLY with JSON."
)

# Issues that are advisory for YAML — values often come from SSM, vault, or env at deploy time.
_YAML_ADVISORY_PATTERNS = re.compile(
    r"s3|aws|bucket|region|secret|credential|password|token|api[_-]?key|"
    r"missing value|empty value|not set|placeholder|public_access|awsbaseurl|"
    r"uat-add|uath-add|prod-add|integration",
    re.IGNORECASE,
)


def _cap_yaml_ai_verdict(verdict: str, summary: str, issues: list[str]) -> str:
    """Never fail YAML solely on missing/external config — warn instead."""
    if verdict != "fail":
        return verdict
    blob = summary + " " + " ".join(issues)
    if _YAML_ADVISORY_PATTERNS.search(blob):
        return "warn"
    return verdict


def analyze_artifact(section: str, label: str, file_path: str, content: str | None) -> dict[str, Any]:
    """Return {available, verdict: pass|warn|fail, summary, issues:[...], confidence}."""
    if content is None:
        body = "(FILE NOT FOUND at the expected path)"
    else:
        body = content[:3500]

    prompt = (
        f"Artifact type: {section}\n"
        f"Service/Portal: {label}\n"
        f"Path: {file_path}\n"
        f"--- BEGIN CONTENT ---\n{body}\n--- END CONTENT ---\n\n"
        "Check for: syntax validity, structural correctness, obviously dangerous or "
        "destructive operations (for SQL: DROP/TRUNCATE/DELETE without guards, missing "
        "liquibase changeset id/author), duplicate or malformed keys (yaml/json), and "
        "anything that would break a merge or deployment.\n"
    )
    if section == "yaml":
        prompt += (
            "IMPORTANT for YAML/parameters.yml:\n"
            "- Missing, empty, or placeholder values for S3, AWS (bucket, region, awsBaseUrl), "
            "secrets, credentials, or env-specific blocks (UAT-ADD, PROD-ADD, etc.) are "
            "ADVISORY ONLY — use verdict \"warn\", never \"fail\". These are often injected "
            "from SSM, Parameter Store, vault, or the deployment pipeline at runtime.\n"
            "- Only use verdict \"fail\" for invalid YAML syntax, duplicate keys at the same "
            "level, or values that would clearly break parsing/merge (not absent external config).\n"
        )
    prompt += (
        "Each entry in `issues` must be developer-actionable: state WHAT is wrong, WHERE "
        "(key/line/statement) and HOW to fix it. If everything looks correct, return an "
        "empty issues list and verdict \"pass\".\n"
        'Respond with JSON exactly: {"verdict":"pass|warn|fail","summary":"one sentence",'
        '"issues":["specific problem + fix", "..."],"confidence":0-100}.'
    )
    raw = _generate(prompt, system=_ANALYZE_SYSTEM, fmt_json=True)
    if raw is None:
        return {"available": False, "verdict": "warn", "summary": "AI analysis unavailable.",
                "issues": [], "confidence": 0}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"available": True, "verdict": "warn",
                "summary": raw[:200] if raw else "Unparseable AI response.",
                "issues": [], "confidence": 0}
    verdict = str(data.get("verdict", "warn")).lower()
    if verdict not in ("pass", "warn", "fail"):
        verdict = "warn"
    issues = data.get("issues") or []
    if isinstance(issues, str):
        issues = [issues]
    issues = [str(i) for i in issues][:8]
    if section == "yaml":
        verdict = _cap_yaml_ai_verdict(verdict, str(data.get("summary", "")), issues)
    return {
        "available": True,
        "verdict": verdict,
        "summary": str(data.get("summary", "")).strip(),
        "issues": issues,
        "confidence": int(data.get("confidence", 0) or 0),
    }


_SUMMARY_SYSTEM = (
    "You are the release captain writing a validation report for the DEVELOPERS who raised a "
    "deployment request (and the Dev Lead who approves it). Given structured validation results, "
    "write a clear, detailed Markdown report that explains exactly what is wrong, why it matters, "
    "and how to fix it — so a developer can act without guessing. Be factual and specific; use the "
    "real service names, branches, file paths and MR numbers from the data. Never invent data.\n"
    "Verdict rules: use APPROVE-READY when overall_status is passed; NEEDS ATTENTION when "
    "overall_status is warnings (advisory items only); BLOCKED only when overall_status is failed. "
    "Missing S3/AWS/secret values in YAML are advisory — they do not block deployment. "
    "Approved Liquibase changesets whose SQL is fully commented out are advisory warnings only — "
    "they do not block deployment."
)


def summarize_report(facts: dict[str, Any]) -> dict[str, Any]:
    """Return {available, markdown}. `facts` is the structured report (minus the summary)."""
    # Slim payload: checks already shown in UI — summary only needs highlights.
    slim_sections = []
    for g in facts.get("sections", []):
        slim_sections.append({
            "section": g.get("section"),
            "title": g.get("title"),
            "status": g.get("status"),
            "items": [
                {
                    "label": it.get("label"),
                    "status": it.get("status"),
                    "checks": [
                        {"name": c.get("name"), "status": c.get("status"), "detail": (c.get("detail") or "")[:200]}
                        for c in (it.get("checks") or [])
                        if c.get("status") in ("fail", "warn")
                    ],
                }
                for it in (g.get("items") or [])
            ],
        })
    slim = {
        "environment": facts.get("environment"),
        "jira_id": facts.get("jira_id"),
        "overall_status": facts.get("overall_status"),
        "stats": facts.get("stats"),
        "sections": slim_sections,
        "steps": facts.get("steps", [])[:8],
    }
    prompt = (
        "Here is the validation result as JSON:\n```json\n"
        + json.dumps(slim, indent=2)[:5000]
        + "\n```\n\nWrite a concise Markdown report (shorter is better):\n"
        "## Verdict — APPROVE-READY, NEEDS ATTENTION, or BLOCKED + one sentence.\n"
        "## What this deploys — 2-3 sentences max.\n"
        "## Findings — bullet per failed/warn item only; note advisory vs blocking.\n"
        "## Recommended next steps — numbered, max 5 items.\n"
        "Skip passing items. YAML S3/AWS gaps are advisory only."
    )
    raw = _generate(prompt, system=_SUMMARY_SYSTEM, num_predict=_NUM_PREDICT_SUMMARY)
    if raw is None:
        return {"available": False, "markdown": ""}
    return {"available": True, "markdown": raw}
