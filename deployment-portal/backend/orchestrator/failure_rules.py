"""
failure_rules.py — Deterministic Jenkins-failure classification (rules first).

Decision priority (highest first):
  1. user_aborted  — Jenkins UI stop ("Aborted by <name>", result=ABORTED). NEVER retry.
  2. build_error   — compile / test / lint failures. Code fix needed. NEVER retry.
  3. dependency_error / config_error — NEVER retry.
  4. heap_oom      — real OOM / Gradle daemon crash with heap-dump flags. RETRY.
  5. transient_infra — network / agent glitch. RETRY.

Gradle's "user interrupt" in daemon logs is NOT a Jenkins manual abort.
"""

from __future__ import annotations

import re
from typing import Any

# ── Jenkins-level manual abort only (100% confidence when matched) ──────────
_JENKINS_ABORT_BY = re.compile(r"Aborted by\s+([^\n\r]+)", re.IGNORECASE)
_JENKINS_ABORT_MARKERS = re.compile(
    r"Build was aborted|Finished:\s*ABORTED|marked build as aborted|"
    r"queue item was cancell?ed|Build was cancell?ed|"
    r"canceled due to context cancellation|Cancelled:\s*context canceled",
    re.IGNORECASE,
)

# ── Code / build failures (never retry) ─────────────────────────────────────
_BUILD_ERROR = re.compile(
    r"COMPILATION ERROR|cannot find symbol|symbol:\s+method|symbol:\s+variable|"
    r"BUILD FAILURE|Tests run:.*Failures:\s*[1-9]|There (are|were) test failures|"
    r"error TS\d+|SyntaxError|AssertionError|FAILED\s+tests?|"
    r"Execution failed for task|Task :.* FAILED",
    re.IGNORECASE,
)

_DEPENDENCY_ERROR = re.compile(
    r"Could not resolve dependencies|Could not find artifact|"
    r"npm ERR!.*(404|ENOTFOUND)|Failed to fetch|"
    r"manifest unknown|pull access denied|denied: requested access",
    re.IGNORECASE,
)

_CONFIG_ERROR = re.compile(
    r"missing (required )?parameter|invalid parameter|"
    r"RELEASE_TAG.*(missing|empty|not set)|unknown environment|"
    r"No such property|Bad credentials|403 Forbidden|401 Unauthorized",
    re.IGNORECASE,
)

# ── Memory / heap (retry) ───────────────────────────────────────────────────
_HEAP_OOM_DIRECT = re.compile(
    r"OutOfMemoryError|Java heap space|GC overhead limit|"
    r"Container killed.*OOM|npm ERR!\s+errno\s+134|"
    r"FATAL ERROR:\s*Ineffective mark-compacts|heap out of memory",
    re.IGNORECASE,
)

_GRADLE_DAEMON_GONE = re.compile(
    r"Gradle build daemon disappeared unexpectedly|"
    r"Daemon vm is shutting down|daemon has exited normally|"
    r"----- End of the daemon log -----",
    re.IGNORECASE,
)

_HEAP_DUMP_FLAG = re.compile(r"HeapDumpOnOutOfMemoryError", re.IGNORECASE)

# ── Transient infra (retry) ─────────────────────────────────────────────────
_TRANSIENT_INFRA = re.compile(
    r"Connection reset|Connection refused|Read timed out|"
    r"\bETIMEDOUT\b|\b503 Service Unavailable\b|\b502 Bad Gateway\b|"
    r"channel is closing|agent went offline|Slave went offline|"
    r"no route to host|Temporary failure in name resolution",
    re.IGNORECASE,
)

# Categories that must never auto-retry regardless of agent policy.
_NEVER_RETRY = frozenset({
    "user_aborted", "build_error", "config_error", "dependency_error", "unknown",
})

# Categories eligible for auto-retry (agent config may still cap attempts).
_RETRYABLE_CATEGORIES = frozenset({"heap_oom", "transient_infra"})


def is_retryable_category(category: str) -> bool:
    return category in _RETRYABLE_CATEGORIES


def classify(console_tail: str, *, jenkins_result: str | None = None) -> dict[str, Any]:
    """Deterministic classification. Returns category, retryable, matched, confidence, evidence."""
    text = console_tail or ""

    # 1) Jenkins manual abort — 100% confidence
    abort = _detect_jenkins_manual_abort(text, jenkins_result)
    if abort:
        return abort

    # 2) Code / build errors — never retry (before heap: compile failure is not OOM)
    m = _BUILD_ERROR.search(text)
    if m:
        return _result("build_error", False, 92, m.group(0)[:120])

    m = _DEPENDENCY_ERROR.search(text)
    if m:
        return _result("dependency_error", False, 90, m.group(0)[:120])

    m = _CONFIG_ERROR.search(text)
    if m:
        return _result("config_error", False, 90, m.group(0)[:120])

    # 3) Heap / OOM — retry
    heap = _detect_heap_oom(text)
    if heap:
        return heap

    m = _TRANSIENT_INFRA.search(text)
    if m:
        return _result("transient_infra", True, 88, m.group(0)[:120])

    return _result("unknown", False, 0, "", matched=False)


def _detect_jenkins_manual_abort(text: str, jenkins_result: str | None) -> dict[str, Any] | None:
    """Jenkins UI abort only — not Gradle daemon 'user interrupt'."""
    who = _abort_actor(text)
    if who:
        return _result(
            "user_aborted", False, 100,
            f"Aborted by {who}",
        )
    if (jenkins_result or "").upper() == "ABORTED":
        return _result("user_aborted", False, 100, "Jenkins result=ABORTED")
    if _JENKINS_ABORT_MARKERS.search(text):
        return _result("user_aborted", False, 98, _JENKINS_ABORT_MARKERS.search(text).group(0)[:120])
    return None


def _detect_heap_oom(text: str) -> dict[str, Any] | None:
    """OOM / Gradle daemon memory crash. Gradle 'user interrupt' alone is NOT an abort."""
    m = _HEAP_OOM_DIRECT.search(text)
    if m:
        return _result("heap_oom", True, 95, m.group(0)[:120])

    # Gradle daemon died with heap-dump JVM flag (e.g. -XX:+HeapDumpOnOutOfMemoryError, -Xmx512m)
    if _HEAP_DUMP_FLAG.search(text) and _GRADLE_DAEMON_GONE.search(text):
        return _result(
            "heap_oom", True, 93,
            "Gradle daemon shutdown with HeapDumpOnOutOfMemoryError (likely OOM)",
        )

    m = _GRADLE_DAEMON_GONE.search(text)
    if m and not _BUILD_ERROR.search(text):
        return _result("heap_oom", True, 85, m.group(0)[:120])

    if re.search(r"Build terminated due to high memory usage", text, re.IGNORECASE):
        return _result("heap_oom", True, 88, "Build terminated due to high memory usage")

    return None


def _abort_actor(console_tail: str) -> str | None:
    m = _JENKINS_ABORT_BY.search(console_tail or "")
    return m.group(1).strip() if m else None


def _result(
    category: str,
    retryable: bool,
    confidence: int,
    evidence: str,
    *,
    matched: bool = True,
) -> dict[str, Any]:
    return {
        "category": category,
        "retryable": retryable and category in _RETRYABLE_CATEGORIES,
        "matched": matched,
        "confidence": confidence,
        "evidence": evidence,
    }


def report_for_category(category: str, console_tail: str) -> dict[str, Any]:
    """Curated human report when rules classify — avoids misleading AI guesses."""
    if category == "user_aborted":
        who = _abort_actor(console_tail)
        if who:
            summary = f"Build manually stopped in Jenkins by {who}."
        else:
            summary = "Build manually stopped in Jenkins before it could finish."
        return {
            "summary": summary,
            "root_cause": (
                "A user clicked Abort in Jenkins. This is not a code defect, "
                "memory failure, or infrastructure outage."
            ),
            "remediation_steps": [
                "No fix required — the stop was intentional.",
                "Re-run the deployment from the portal when ready.",
                "Automatic retry is skipped for manual aborts.",
            ],
        }
    if category == "heap_oom":
        return {
            "summary": "Build failed due to insufficient memory (Gradle/JVM or Node heap).",
            "root_cause": (
                "The build process ran out of memory — e.g. Gradle daemon crash, "
                "Java heap space, or Node max-old-space-size exceeded."
            ),
            "remediation_steps": [
                "Retry the build — OOM on a busy agent often succeeds on retry.",
                "Ask DevOps to increase Gradle JVM heap (-Xmx) or Node --max-old-space-size on the agent.",
                "Check for memory-heavy parallel builds on the same Jenkins agent.",
            ],
        }
    if category == "build_error":
        return {
            "summary": "Build failed due to a code or test error (not infrastructure).",
            "root_cause": (
                "Compilation, unit test, or lint failure in the source code. "
                "Retrying without a code fix will not help."
            ),
            "remediation_steps": [
                "Fix the compile/test errors shown in the Jenkins console.",
                "Push the fix to the branch and submit a new deployment request.",
                "Automatic retry is skipped for code-level failures.",
            ],
        }
    if category == "dependency_error":
        return {
            "summary": "Build failed resolving dependencies or artifacts.",
            "root_cause": "Maven/npm/Docker could not fetch a required artifact or image.",
            "remediation_steps": [
                "Verify registry credentials and artifact version exists.",
                "Retry if a transient registry outage caused the failure.",
            ],
        }
    if category == "config_error":
        return {
            "summary": "Build failed due to invalid or missing Jenkins/portal parameters.",
            "root_cause": "Wrong RELEASE_TAG, environment, or Jenkins credential.",
            "remediation_steps": [
                "Verify RELEASE_TAG and build parameters in the portal.",
                "Check Jenkins job configuration and credentials.",
            ],
        }
    if category == "transient_infra":
        return {
            "summary": "Build failed due to a transient infrastructure issue.",
            "root_cause": "Network, Jenkins agent, or registry glitch during the build.",
            "remediation_steps": [
                "Retry the build — transient issues often clear on a second attempt.",
                "Check Jenkins agent health and network connectivity.",
            ],
        }
    return {
        "summary": "",
        "root_cause": "",
        "remediation_steps": [],
    }
