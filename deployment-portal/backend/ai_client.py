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
from typing import Any

import httpx

BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
_PREFERRED_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "90"))

_resolved_model: str | None = None


def _list_models() -> list[str]:
    try:
        r = httpx.get(f"{BASE_URL}/api/tags", timeout=4)
        if r.status_code != 200:
            return []
        return [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]
    except Exception:
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


def _generate(prompt: str, *, system: str | None = None, fmt_json: bool = False) -> str | None:
    model = resolve_model()
    if not model:
        return None
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2, "num_ctx": 8192},
    }
    if system:
        payload["system"] = system
    if fmt_json:
        payload["format"] = "json"
    try:
        r = httpx.post(f"{BASE_URL}/api/generate", json=payload, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except Exception:
        return None


_ANALYZE_SYSTEM = (
    "You are a meticulous release-engineering reviewer for a deployment portal. "
    "You inspect a single configuration/migration/phrases artifact and judge whether it is "
    "safe to merge and deploy. Be concise, specific and actionable. Respond ONLY with JSON."
)


def analyze_artifact(section: str, label: str, file_path: str, content: str | None) -> dict[str, Any]:
    """Return {available, verdict: pass|warn|fail, summary, issues:[...], confidence}."""
    if content is None:
        body = "(FILE NOT FOUND at the expected path)"
    else:
        body = content[:6000]

    prompt = (
        f"Artifact type: {section}\n"
        f"Service/Portal: {label}\n"
        f"Path: {file_path}\n"
        f"--- BEGIN CONTENT ---\n{body}\n--- END CONTENT ---\n\n"
        "Check for: syntax validity, structural correctness, obviously dangerous or "
        "destructive operations (for SQL: DROP/TRUNCATE without guards, missing "
        "liquibase changeset id/author), duplicate or malformed keys (yaml/json), and "
        "anything that would break a merge or deployment.\n"
        'Respond with JSON exactly: {"verdict":"pass|warn|fail","summary":"one sentence",'
        '"issues":["..."],"confidence":0-100}.'
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
    return {
        "available": True,
        "verdict": verdict,
        "summary": str(data.get("summary", "")).strip(),
        "issues": [str(i) for i in issues][:8],
        "confidence": int(data.get("confidence", 0) or 0),
    }


_SUMMARY_SYSTEM = (
    "You are the release captain writing an approval briefing for a Development Lead. "
    "Given structured validation results for a deployment request, write a short, well-organised "
    "Markdown report that lets the lead approve or reject with confidence. Be factual; do not invent data."
)


def summarize_report(facts: dict[str, Any]) -> dict[str, Any]:
    """Return {available, markdown}. `facts` is the structured report (minus the summary)."""
    prompt = (
        "Here is the validation result as JSON:\n```json\n"
        + json.dumps(facts, indent=2)[:9000]
        + "\n```\n\nWrite a Markdown briefing with these sections, in order:\n"
        "## Verdict — one line: APPROVE-READY, NEEDS ATTENTION, or BLOCKED, with a one-sentence reason.\n"
        "## What this deploys — plain-English summary of the services/sections and branches involved.\n"
        "## Checks — a compact bullet list of what passed and what failed (mergeability, file presence, content checks).\n"
        "## Risks & insights — anything the lead should weigh (destructive SQL, conflicts, missing files).\n"
        "## Recommended next steps — numbered, concrete actions.\n"
        "Keep it under ~250 words. Use the real service names and URLs from the data."
    )
    raw = _generate(prompt, system=_SUMMARY_SYSTEM)
    if raw is None:
        return {"available": False, "markdown": ""}
    return {"available": True, "markdown": raw}
