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
import time
from dataclasses import dataclass
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


_DB_SQL_REVIEW_SYSTEM = (
    "You are an expert MySQL SQL syntax reviewer for Liquibase migration scripts.\n"
    "You receive one or more Approved changesets, each shown as numbered SQL lines and\n"
    "delimited by a `### Changeset author:id` header.\n\n"
    "IGNORE (never report as errors):\n"
    "- `--liquibase formatted sql` and `-- Changeset author:id labels:...` header lines\n"
    "- Block/line comments starting with `--` (author notes, jira refs, dates, commented-out SQL)\n"
    "- Business logic / data correctness\n\n"
    "DO report EVERY real syntax error in executable (non-comment) SQL:\n"
    "- Misspelled keywords: SELEC→SELECT, ISERT/INSET→INSERT, WHRE→WHERE, ELECT→SELECT\n"
    "- Missing `=` in predicates: `Key` 'value' must be `Key` = 'value'\n"
    "- Missing comma between column names: (`ColA` `ColB`) must be (`ColA`, `ColB`)\n"
    "- Unbalanced parentheses, broken quotes, missing semicolons, invalid INSERT…SELECT shape\n\n"
    "Critical rules:\n"
    "- Review EVERY changeset independently. Do not stop after the first one.\n"
    "- Report each changeset that has an error; omit changesets that are fully valid.\n"
    "- If INSERT/UPDATE/SELECT/DELETE statements appear, the changeset is NOT empty\n"
    "- Never say 'empty changeset' when SQL statements are present\n"
    "- Never suggest removing `--` comment lines\n"
    "- Use the exact line number prefix from each changeset's snippet (e.g. `18:` → line 18)\n"
    "- message: one concise error description\n"
    "- hint: one specific fix sentence (what to add/change) — do NOT paste the whole SQL line\n"
    "- If all changesets are valid, return {\"findings\":{}}\n"
    "Respond ONLY with JSON."
)


def db_sql_review_enabled() -> bool:
    """Ollama SQL syntax review for Approved Liquibase changesets (default on with AI)."""
    return os.getenv("VALIDATION_DB_SQL_AI", "true").lower() in ("1", "true", "yes")


def db_typo_ai_enabled() -> bool:
    """Backward-compatible alias for ``db_sql_review_enabled``."""
    return db_sql_review_enabled()


def _line_from_snippet(snippet: str, line_no: int) -> str:
    """Extract one source line from a numbered snippet."""
    prefix = f"{line_no}:"
    for row in (snippet or "").splitlines():
        if row.strip().startswith(prefix):
            return row.split(":", 1)[-1].strip()
    return ""


def _deterministic_hint_from_message(message: str, line: int = 0) -> str:
    """Build a fix hint from a typo-style or missing-operator message."""
    m = re.match(r"typo `([^`]+)` — did you mean `([^`]+)`\?", message.strip())
    if m:
        return f"Change `{m.group(1)}` to `{m.group(2)}`."
    if "missing" in message.lower() and "=" in message.lower():
        return "Add `=` between the column name and the string literal."
    return ""


def _polish_sql_finding(item: dict[str, Any], *, snippet: str = "") -> dict[str, Any]:
    """Normalize Ollama message/hint for the UI (clarity, no SQL dumps, no line prefixes)."""
    line = int(item.get("line") or 0)
    msg = str(item.get("message") or "").strip()
    hint = str(item.get("hint") or "").strip()
    wrong = str(item.get("wrong") or "").strip()
    correct = str(item.get("correct") or "").strip()

    low_msg = msg.lower()
    if wrong and correct and "typo" not in low_msg:
        msg = f"typo `{wrong}` — did you mean `{correct}`?"

    src_line = _line_from_snippet(snippet, line) if line else ""

    if hint and re.match(r"^\d+:\s*(INSERT|UPDATE|SELECT|DELETE)\b", hint, re.I):
        hint = ""

    if ("missing" in low_msg and "=" in low_msg) or re.search(r"`Key`\s+'", src_line):
        if not msg or "missing" in low_msg:
            msg = (
                f"Missing `=` operator in `{src_line}`"
                if src_line else "Missing `=` operator in WHERE predicate"
            )
        if not hint or len(hint) < 24:
            hint = "Add `=` between `Key` and the string literal."

    if "misspelled" in low_msg or "typo" in low_msg or "keyword" in low_msg:
        if not (wrong and correct):
            m = (
                re.search(r"'(\w+)'\s+instead of\s+'(\w+)'", msg, re.I)
                or re.search(r"change\s+['\"`]?(\w+)['\"`]?\s+to\s+['\"`]?(\w+)['\"`]?", hint, re.I)
            )
            if m:
                wrong, correct = m.group(1), m.group(2)
        if wrong and correct:
            # Name the keyword in both the message and the hint (backticks, not quotes).
            if "`" not in msg:
                msg = f"Misspelled keyword `{wrong}` — did you mean `{correct}`?"
            hint = f"Change `{wrong}` to `{correct}`."

    if "comma" in low_msg and not hint:
        hint = "Add a missing comma between column names."

    if not hint:
        hint = _deterministic_hint_from_message(msg, line)

    # Strip any "on/near line N" the model may have added — header shows location.
    hint = re.sub(r"\s+(?:on|near)\s+line\s+\d+\.?\s*$", "", hint, flags=re.I).rstrip(".")
    if hint and hint[0].islower():
        hint = hint[0].upper() + hint[1:]
    if hint and not hint.endswith("."):
        hint += "."

    item["line"] = line
    item["message"] = msg
    item["hint"] = hint
    return item


def _parse_sql_review_findings(
    data: Any,
    *,
    snippet: str = "",
    snippet_by_cs: dict[str, str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Normalize Ollama JSON into changeset → list of {line, message, hint}.

    ``snippet_by_cs`` supplies the numbered SQL per changeset for batched reviews so
    each finding is polished against its own source line; ``snippet`` is the
    single-changeset fallback.
    """
    raw = data.get("findings") if isinstance(data, dict) else data
    if not isinstance(raw, dict):
        return {}

    out: dict[str, list[dict[str, Any]]] = {}
    for cs_key, item in raw.items():
        if not cs_key:
            continue
        cs_snippet = (snippet_by_cs or {}).get(str(cs_key), snippet)
        rows: list[Any] = item if isinstance(item, list) else ([item] if isinstance(item, dict) else [])
        parsed: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            wrong = str(row.get("wrong") or "").strip()
            correct = str(row.get("correct") or "").strip()
            msg = str(row.get("message") or "").strip()
            if not msg and wrong and correct:
                msg = f"typo `{wrong}` — did you mean `{correct}`?"
            if not msg:
                continue
            # Contexts only ever contain changesets with executable SQL, so any
            # "empty changeset/statement" finding is a hallucination — drop it.
            if re.search(r"\bempty\b.*\b(change ?set|statement)\b", msg, re.I):
                continue
            line = int(row.get("line") or 0)
            hint = str(row.get("hint") or "").strip()
            parsed.append(_polish_sql_finding(
                {"line": line, "message": msg, "hint": hint, "wrong": wrong, "correct": correct},
                snippet=cs_snippet,
            ))
        if parsed:
            out[str(cs_key)] = parsed
    return out


def _review_one_changeset(
    label: str,
    ctx: dict[str, Any],
    *,
    environment: str,
    dialect: str,
    predict: int,
) -> list[dict[str, Any]] | None:
    prompt = (
        f"Service: {label}\n"
        f"Changeset: {ctx['changeset']}\n"
        f"File: {ctx.get('file', '')}\n"
        f"Environment: {environment}  |  Dialect: {dialect}\n\n"
        "Numbered SQL (use these line numbers in your response):\n"
        f"{(ctx.get('sql_snippet') or '').strip()}\n\n"
        "Report EVERY real syntax error in executable SQL (typos, missing commas, "
        "missing semicolons, bad quotes). Do NOT stop after the first error.\n"
        "If the SQL is fully valid, return empty findings.\n"
        'JSON: {"findings":{"'
        + str(ctx["changeset"]).replace('"', '\\"')
        + '":[{"line":N,"message":"...","hint":"..."},...]}}  or {"findings":{}} if valid.'
    )
    raw = _generate(prompt, system=_DB_SQL_REVIEW_SYSTEM, fmt_json=True, num_predict=predict)
    if not raw:
        return None
    try:
        parsed = _parse_sql_review_findings(json.loads(raw), snippet=ctx.get("sql_snippet") or "")
        cs = ctx["changeset"]
        if cs in parsed:
            return parsed[cs]
        for items in parsed.values():
            if items:
                return items
        return []
    except json.JSONDecodeError:
        return None


def review_one_db_changeset(
    label: str,
    ctx: dict[str, Any],
    *,
    environment: str = "INTEG",
    dialect: str = "mysql",
) -> list[dict[str, Any]] | None:
    """Review a single changeset (used for safety re-review)."""
    predict = int(os.getenv("VALIDATION_DB_SQL_NUM_PREDICT", "1024"))
    return _review_one_changeset(
        label, ctx, environment=environment, dialect=dialect, predict=predict,
    )


@dataclass
class DbSqlReviewResult:
    findings: dict[str, list[dict[str, Any]]]
    ollama_failed: set[str]


def review_db_sql_syntax(
    label: str,
    contexts: list[dict[str, Any]],
    *,
    environment: str = "INTEG",
    dialect: str = "mysql",
) -> DbSqlReviewResult | None:
    """Review flagged changesets with one Ollama call each, within a time budget.

    Calls are per-changeset (never batched) so line numbers can never leak between
    changesets. A wall-clock budget (``VALIDATION_DB_SQL_BUDGET_S``, default 12s) keeps
    the whole step fast: once it is exhausted, any remaining changeset is returned in
    ``ollama_failed`` so the caller reports it deterministically (changeset + line).
    """
    if not contexts or not db_sql_review_enabled():
        return DbSqlReviewResult(findings={}, ollama_failed=set())

    predict = int(os.getenv("VALIDATION_DB_SQL_NUM_PREDICT", "320"))
    budget = float(os.getenv("VALIDATION_DB_SQL_BUDGET_S", "10"))
    out: dict[str, list[dict[str, Any]]] = {}
    failed: set[str] = set()
    start = time.monotonic()
    for ctx in contexts[:12]:
        cs = ctx["changeset"]
        if time.monotonic() - start > budget:
            failed.add(cs)  # out of time -> deterministic fallback keeps it fast
            continue
        items = _review_one_changeset(
            label, ctx, environment=environment, dialect=dialect, predict=predict,
        )
        if items is None:
            failed.add(cs)
            continue
        if items:
            out[cs] = items
    return DbSqlReviewResult(findings=out, ollama_failed=failed)


_DB_EXPLAIN_SYSTEM = (
    "You are a Liquibase/MySQL SQL reviewer. Given SQL syntax errors with source snippets, "
    "write one short fix sentence per changeset. Be specific. Do NOT suggest removing comments. "
    "Respond ONLY with JSON."
)


def detect_db_sql_typos(
    label: str,
    contexts: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Deprecated — use ``review_db_sql_syntax``. Kept for compatibility."""
    flat: dict[str, dict[str, Any]] = {}
    result = review_db_sql_syntax(label, contexts)
    if not result:
        return flat
    for cs, items in result.findings.items():
        if items:
            flat[cs] = items[0]
    return flat


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


_YAML_REVIEW_SYSTEM = (
    "You are an expert reviewer for Forever Living deployment YAML (parameters.yml).\n\n"
    "File structure:\n"
    "- Root env blocks: INTEG-ADD, UAT-ADD, UATH-ADD, SUPPORT-ADD, PROD-ADD, COMMON-ADD, COMMON-REMOVEKEY\n"
    "- Header comment blocks starting with # or ## are never errors\n\n"
    "Your job: write clear REMEDIATION hints for issues the deterministic validator already found.\n"
    "Rules:\n"
    "- Use the EXACT line numbers from the numbered snippet and from DETERMINISTIC ISSUES — never invent lines\n"
    "- Each hint: one concrete fix (what to type/change), max 140 chars, no full-line paste\n"
    "- For indent errors: say how many spaces to use and reference a sibling block that is correct\n"
    "- For colon errors: show corrected `key: value` form\n"
    "- Missing S3/AWS/secrets/empty placeholders → never blocking; omit unless asked\n"
    "- Do NOT contradict deterministic findings; do NOT add new blocking issues unless deterministic missed "
    "a true parse break\n"
    "- If no issues, return empty hints list\n"
    "Respond ONLY with JSON."
)


def yaml_review_enabled() -> bool:
    """Ollama remediation hints for YAML validation failures (default on with AI)."""
    return os.getenv("VALIDATION_YAML_AI", "true").lower() in ("1", "true", "yes")


def _parse_yaml_review_hints(data: Any) -> list[dict[str, Any]]:
    """Normalize Ollama YAML hint JSON."""
    if not isinstance(data, dict):
        return []
    raw = data.get("hints") or data.get("remediation") or []
    if isinstance(raw, dict):
        rows: list[dict[str, Any]] = []
        for key, val in raw.items():
            if isinstance(val, dict):
                rows.append(val)
            elif val:
                m = re.match(r"^(\d+)", str(key))
                rows.append({"line": int(m.group(1)) if m else 0, "fix": str(val).strip()})
        raw = rows
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for row in raw[:12]:
        if not isinstance(row, dict):
            continue
        line = int(row.get("line") or 0)
        fix = str(row.get("fix") or row.get("hint") or row.get("remediation") or "").strip()
        if not fix:
            continue
        fix = re.sub(r"\s+(?:on|near)\s+line\s+\d+\.?\s*$", "", fix, flags=re.I).rstrip(".")
        if fix and not fix.endswith("."):
            fix += "."
        out.append({
            "line": line,
            "block": str(row.get("block") or "").strip(),
            "message": str(row.get("message") or "").strip(),
            "fix": fix,
        })
    return out


def review_yaml_file(
    label: str,
    filename: str,
    content: str,
    *,
    environment: str = "",
    deterministic_issues: list[dict[str, Any]] | None = None,
    numbered_snippet: str = "",
) -> dict[str, Any]:
    """Return remediation hints for deterministic YAML issues (never changes pass/fail)."""
    empty: dict[str, Any] = {
        "available": False,
        "summary": "AI YAML review unavailable.",
        "hints": [],
        "confidence": 0,
    }
    if not content or not content.strip():
        return {**empty, "summary": "No YAML content to review."}
    if not yaml_review_enabled():
        return empty

    det = deterministic_issues or []
    det_json = json.dumps(det[:12], indent=2) if det else "none"
    snippet = numbered_snippet or content[:3500]
    env = (environment or "INTEG").strip().upper()

    prompt = (
        f"Service: {label}\n"
        f"File: {filename}\n"
        f"Target deploy environment: {env}\n"
        f"Expected root block: {env}-* or COMMON-*\n\n"
        f"DETERMINISTIC ISSUES (must explain these — same line numbers):\n{det_json}\n\n"
        "Numbered YAML (line numbers for your response):\n"
        f"{snippet}\n\n"
        "For EACH deterministic issue, return one remediation hint with the same line number.\n"
        "JSON exactly:\n"
        '{"summary":"one sentence overview","hints":[{"line":N,"block":"INTEG-ADD",'
        '"message":"short restatement","fix":"specific fix sentence"}],"confidence":0-100}'
    )
    predict = int(os.getenv("VALIDATION_YAML_NUM_PREDICT", "512"))
    raw = _generate(prompt, system=_YAML_REVIEW_SYSTEM, fmt_json=True, num_predict=predict)
    if raw is None:
        return empty
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "available": True,
            "summary": raw[:200] if raw else "Unparseable AI YAML review.",
            "hints": [],
            "confidence": 0,
        }
    hints = _parse_yaml_review_hints(data)
    return {
        "available": True,
        "summary": str(data.get("summary", "")).strip(),
        "hints": hints,
        "confidence": int(data.get("confidence", 0) or 0),
    }


def merge_yaml_hints(
    issues: list[dict[str, Any]],
    ai_hints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Overlay AI fix sentences onto deterministic issue rows (match by line number)."""
    if not issues:
        return issues
    by_line: dict[int, str] = {}
    for h in ai_hints:
        line = int(h.get("line") or 0)
        fix = str(h.get("fix") or "").strip()
        if line and fix and not _yaml_hint_is_generic(fix):
            by_line[line] = fix
    merged: list[dict[str, Any]] = []
    for row in issues:
        line = int(row.get("line") or 0)
        det_hint = str(row.get("hint") or "").strip()
        ai_hint = by_line.get(line, "")
        hint = ai_hint or det_hint
        merged.append({**row, "hint": hint})
    return merged


def _yaml_hint_is_generic(fix: str) -> bool:
    """Reject vague AI hints that repeat the same text for every line."""
    text = fix.strip()
    low = text.lower()
    if re.match(r"^use\s+`?key:\s*value`?\s*format\.?$", low):
        return True
    if len(text) < 40 and "key: value" in low and "change" not in low:
        return True
    return False


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


_JENKINS_LOG_SYSTEM = (
    "You are a senior release/build engineer triaging a FAILED Jenkins build. "
    "You receive build parameters, the Jenkins result field, optional rule-based "
    "pre-classification, and the console log.\n\n"
    "STRICT DECISION RULES (follow in order):\n"
    "1. user_aborted — ONLY when Jenkins UI stopped the build: lines like "
    "'Aborted by <person name>', 'Build was aborted', 'Finished: ABORTED', or "
    "jenkins_result=ABORTED. NEVER retry. "
    "Gradle daemon text 'user interrupt' is NOT a Jenkins abort.\n"
    "2. build_error — compile errors, test failures, lint, 'BUILD FAILURE', "
    "'cannot find symbol', 'Task :... FAILED'. Code must be fixed. NEVER retry.\n"
    "3. dependency_error / config_error — artifact/registry/parameter issues. NEVER retry.\n"
    "4. heap_oom — OutOfMemoryError, Java heap space, Gradle daemon disappeared, "
    "HeapDumpOnOutOfMemoryError with daemon shutdown, Node heap exceeded. RETRYABLE.\n"
    "5. transient_infra — network timeout, agent offline, HTTP 5xx. RETRYABLE.\n"
    "6. unknown — if unclear. NEVER retry.\n\n"
    "Set retryable=true ONLY for heap_oom or transient_infra. "
    "If rules pre-classified with confidence >= 90, agree unless log clearly contradicts. "
    "Respond ONLY with JSON."
)

# Failure categories the orchestrator understands. `retryable` categories are the
# only ones the graph will loop back and re-run automatically (capped by max_retries).
FAILURE_CATEGORIES = (
    "user_aborted",     # manual stop in Jenkins — NOT retryable
    "heap_oom",         # OutOfMemoryError / Java heap space / GC overhead — retryable
    "transient_infra",  # network reset, timeout, agent offline, 5xx — retryable
    "build_error",      # compile/test/lint failure — needs a code fix, NOT retryable
    "config_error",     # bad param, missing RELEASE_TAG, wrong env — needs fix, NOT retryable
    "dependency_error", # artifact/registry/dependency resolution — usually NOT retryable
    "unknown",          # could not classify — do not auto-retry
)


def analyze_jenkins_log(
    service_label: str,
    jenkins_params: dict[str, Any],
    console_tail: str,
    build_number: int | None = None,
    jenkins_result: str | None = None,
    rule_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ollama triage of a failed Jenkins build.

    Returns a structured report:
        {
          "available": bool,
          "category": one of FAILURE_CATEGORIES,
          "retryable": bool,          # AI's opinion; orchestrator still applies its own policy
          "confidence": 0-100,
          "summary": str,             # one-line human summary
          "root_cause": str,
          "remediation_steps": [str, ...],
        }

    Best-effort: if Ollama is unavailable the caller still gets a deterministic
    fallback (see orchestrator failure rules) so the pipeline never hard-depends on AI.
    """
    fallback = {
        "available": False,
        "category": "unknown",
        "retryable": False,
        "confidence": 0,
        "summary": "AI log analysis unavailable — using deterministic classification.",
        "root_cause": "",
        "remediation_steps": [],
    }
    if not console_tail or not console_tail.strip():
        return {**fallback, "summary": "No console output captured to analyze."}

    if (jenkins_result or "").upper() == "ABORTED":
        return {
            "available": False,
            "category": "user_aborted",
            "retryable": False,
            "confidence": 100,
            "summary": "Build manually stopped in Jenkins (result=ABORTED).",
            "root_cause": "Manual abort — not a code or infrastructure failure.",
            "remediation_steps": [
                "Re-run the deployment when ready; no automatic retry.",
            ],
        }

    # High-confidence rule hit — skip Ollama for category; rules already decided retry.
    if rule_hint and rule_hint.get("matched") and int(rule_hint.get("confidence", 0)) >= 90:
        cat = rule_hint.get("category", "unknown")
        return {
            "available": False,
            "category": cat,
            "retryable": bool(rule_hint.get("retryable")),
            "confidence": int(rule_hint.get("confidence", 0)),
            "summary": "",
            "root_cause": "",
            "remediation_steps": [],
        }

    tail = console_tail[-8000:]
    params_line = ", ".join(f"{k}={v}" for k, v in (jenkins_params or {}).items())
    result_line = f"Jenkins result field: {jenkins_result or 'unknown'}\n"
    rule_line = ""
    if rule_hint and rule_hint.get("matched"):
        rule_line = (
            f"Rule pre-classification: {rule_hint.get('category')} "
            f"(confidence {rule_hint.get('confidence')}, evidence: {rule_hint.get('evidence', '')[:80]})\n"
        )
    prompt = (
        f"Service: {service_label}\n"
        f"Jenkins build: #{build_number if build_number is not None else '?'}\n"
        f"Build parameters: {params_line}\n"
        f"{result_line}"
        f"{rule_line}\n"
        f"--- CONSOLE LOG (tail) ---\n{tail}\n--- END LOG ---\n\n"
        "Classify using the STRICT DECISION RULES in your system prompt.\n"
        "retryable=true ONLY for heap_oom or transient_infra.\n"
        "build_error / user_aborted / config / dependency → retryable=false.\n"
        "remediation_steps: 2-4 short, actionable steps for a developer or DevOps.\n"
        'Respond with JSON exactly: {"category":"...","retryable":true|false,'
        '"confidence":0-100,"summary":"one line","root_cause":"one sentence",'
        '"remediation_steps":["step",...]}'
    )
    raw = _generate(prompt, system=_JENKINS_LOG_SYSTEM, fmt_json=True)
    if raw is None:
        return fallback
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {**fallback, "available": True, "summary": raw[:200]}

    category = str(data.get("category", "unknown")).lower().strip()
    if category not in FAILURE_CATEGORIES:
        category = "unknown"
    retryable = bool(data.get("retryable", False)) and category in ("heap_oom", "transient_infra")
    steps = data.get("remediation_steps") or []
    if isinstance(steps, str):
        steps = [steps]
    return {
        "available": True,
        "category": category,
        "retryable": retryable,
        "confidence": int(data.get("confidence", 0) or 0),
        "summary": str(data.get("summary", "")).strip(),
        "root_cause": str(data.get("root_cause", "")).strip(),
        "remediation_steps": [str(s).strip() for s in steps if str(s).strip()][:5],
    }
