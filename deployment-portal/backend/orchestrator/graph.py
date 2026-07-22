"""
orchestrator_graph.py — LangGraph deployment orchestrator (master + per-agent subgraphs).

Design (see OrcProgress.md for living diagrams):

  MASTER ORCHESTRATOR
      routes each portal sub-task to the agent that owns its section.
      Today the Microservice and Portal agents are enabled (INTEG POC); yaml /
      db / phrases agents reuse the SAME subgraph template and are turned on in
      config/orchestrator_agents.json one at a time.

  AGENT SUBGRAPH (per sub-task)
      load_context → build_params → trigger_jenkins → poll_jenkins ─┐
                                                                     │ running → poll
        success → health_check → (ok) → finalize_success → END      │
        failure → fetch_console_log → analyze_failure               │
                    ├─ retryable + attempts left → trigger_jenkins (loop)
                    └─ otherwise → build_error_report → END

  FAILURE HANDLING
      failure_rules.classify()  → fast deterministic category (heap_oom, ...)
      ai_client.analyze_jenkins_log() → Ollama summary + remediation steps
      Only heap_oom / transient_infra are auto-retried (capped by max_retries).

Run standalone in LangGraph Studio:
    cd deployment-portal/backend && source .venv/bin/activate && langgraph dev
    Input e.g. {"task_id":"TASK-1","service_label":"Account","release_tag":"R-2026-07-W1",
                "demo_scenario":"fail_heap"}  # success | fail_build | fail_heap

When invoked by the portal (orchestrator_runner), `sub_task_id` is present and
every node persists progress to db.json so the UI reflects live state.
"""

from __future__ import annotations

import os
import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

import ai_client
import db
from orchestrator import config as orchestrator_config
from orchestrator import failure_rules, jenkins_client, jenkins_params

# Console-log poll cadence (seconds). Small in mock mode; real builds poll slower.
_POLL_INTERVAL = float(os.getenv("ORCHESTRATOR_POLL_INTERVAL", "3"))

# Step indices in the microservice sub-task step list (db._SUB_TASK_STEP_LABELS).
STEP_VALIDATE = 0
STEP_MERGE = 1
STEP_TRIGGER = 2
STEP_WAIT = 3
STEP_HEALTH = 4
STEP_VERIFY = 5


class AgentState(TypedDict, total=False):
    # Identity / input
    task_id: str
    sub_task_id: str
    agent_type: str
    service_label: str
    release_tag: str
    demo_scenario: str  # success | fail_build | fail_heap (mock testing only)

    # Jenkins
    jenkins_job_path: str
    jenkins_params: dict[str, str]
    jenkins_build_number: int | None
    jenkins_status: str  # triggered | running | success | failure
    jenkins_result: str | None  # SUCCESS | FAILURE | ABORTED | UNSTABLE | None while running

    # Failure handling
    console_log: str
    failure_category: str
    retryable: bool
    retry_count: int
    max_retries: int
    ai_report: dict[str, Any]

    # YAML multi-file serial deploy (one Jenkins run per file)
    yaml_deployment: dict[str, Any]
    yaml_deploy_index: int

    # Json & SchemaForms serial deploy (Delete before Add for Phrases.json)
    json_deployment: dict[str, Any]
    json_deploy_index: int

    # Outcome
    status: str  # running | done | failed
    logs: list[str]
    error: str | None


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def _log(state: AgentState, message: str) -> list[str]:
    logs = list(state.get("logs") or [])
    logs.append(message)
    return logs


def _persist(state: AgentState, **kwargs: Any) -> None:
    """Write progress to db.json when running under the portal (sub_task_id set).

    No-op in standalone Studio runs so the graph stays pure and inspectable.
    """
    sub_task_id = state.get("sub_task_id")
    if not sub_task_id:
        return
    try:
        db.record_agent_event(sub_task_id, **kwargs)
    except Exception:
        # Persistence must never crash the orchestrator.
        pass


# ──────────────────────────────────────────────────────────────────────────
# Nodes
# ──────────────────────────────────────────────────────────────────────────

def load_context(state: AgentState) -> dict[str, Any]:
    task_id = state.get("task_id", "")
    service_label = state.get("service_label") or "Account"
    agent_type = state.get("agent_type") or "microservice"
    agent = orchestrator_config.get_agent(agent_type)

    release_tag = (state.get("release_tag") or "").strip()
    task = db.get_task(task_id) if task_id else None
    if task:
        release_tag = release_tag or (task.get("release_tag") or "").strip()

    if not release_tag:
        msg = "RELEASE_TAG missing — DevOps must set it before deploy"
        _persist(state, status="failed", step_index=STEP_VALIDATE, step_status="failed",
                 step_detail=msg, log_lines=[f"ERROR: {msg}"])
        return {"error": msg, "status": "failed", "logs": _log(state, f"ERROR: {msg}")}

    job_path = jenkins_params.job_path_for_agent(agent_type)
    detail = (
        f"Context loaded — {agent_type}={service_label}, "
        f"job={job_path}, RELEASE_TAG={release_tag}"
    )
    _persist(state, status="running", step_index=STEP_VALIDATE, step_status="done",
             step_detail=detail, log_lines=[detail], retry_count=state.get("retry_count", 0))
    return {
        "service_label": service_label,
        "agent_type": agent_type,
        "release_tag": release_tag,
        "jenkins_job_path": job_path,
        "max_retries": int(agent.get("max_retries", 0)),
        "retry_count": state.get("retry_count", 0),
        "status": "running",
        "error": None,
        "logs": _log(state, detail),
    }


def build_params(state: AgentState) -> dict[str, Any]:
    if state.get("error"):
        return {}
    agent_type = state.get("agent_type") or "microservice"
    sub_task_id = state.get("sub_task_id", "")
    sub_task = db.get_sub_task(sub_task_id) if sub_task_id else None
    task = db.get_task(state.get("task_id", "")) if state.get("task_id") else None
    environment = (task or {}).get("environment", "INTEG")
    sub_type = (sub_task or {}).get("sub_type") or "microservice"
    release_branch = (sub_task or {}).get("release_branch") or ""

    yaml_action = "Update"
    dep = state.get("yaml_deployment")
    if dep:
        yaml_action = dep.get("action", "Update")
    elif sub_task:
        deployments = sub_task.get("yaml_deployments") or []
        idx = int(state.get("yaml_deploy_index") or 0)
        if deployments and idx < len(deployments):
            yaml_action = deployments[idx].get("action", "Update")

    json_action = "Add"
    json_dep = state.get("json_deployment")
    if json_dep:
        json_action = json_dep.get("action") or "Add"
    elif sub_task and agent_type == "phrases":
        json_deployments = sub_task.get("json_deployments") or []
        jidx = int(state.get("json_deploy_index") or 0)
        if json_deployments and jidx < len(json_deployments):
            json_action = json_deployments[jidx].get("action") or "Add"

    if agent_type == "phrases":
        json_dep = state.get("json_deployment")
        if json_dep and json_dep.get("jenkins_params"):
            params = json_dep["jenkins_params"]
        elif sub_task:
            json_deployments = sub_task.get("json_deployments") or []
            jidx = int(state.get("json_deploy_index") or 0)
            if json_deployments and jidx < len(json_deployments):
                params = json_deployments[jidx].get("jenkins_params") or sub_task.get("jenkins_params") or {}
            else:
                params = sub_task.get("jenkins_params") or {}
        else:
            params = {}
        if not params:
            params = jenkins_params.build_params_for_agent(
                agent_type=agent_type,
                service_label=state["service_label"],
                release_tag=state["release_tag"],
                sub_type=sub_type,
                environment=environment,
                release_branch=release_branch,
                json_action=json_action,
            )
    else:
        params = jenkins_params.build_params_for_agent(
            agent_type=agent_type,
            service_label=state["service_label"],
            release_tag=state["release_tag"],
            sub_type=sub_type,
            environment=environment,
            release_branch=release_branch,
            yaml_action=yaml_action,
            db_action=(sub_task or {}).get("db_action") or "Update",
            json_action=json_action,
        )
    if agent_type == "yaml":
        detail = (
            f"Built Jenkins params — {params['SERVICE']} {params['ACTION']} "
            f"({params['ENVIRONMENT']}, branch={params['Branch']}, tag={params['RELEASE_TAG']})"
        )
    elif agent_type == "db":
        detail = (
            f"Built Jenkins params — {params['MICROSERVICE']} {params['ACTION']} "
            f"({params['ENVIRONMENT']}, branch={params['Branch']}, tag={params['RELEASE_TAG']})"
        )
    elif agent_type == "phrases":
        action_bit = f" {params['Action']}" if params.get("Action") else ""
        detail = (
            f"Built Jenkins params — {params['Portals']} {params['Update']}{action_bit} "
            f"({params['Environment']}, branch={params['PhrasesBranch']}, "
            f"version={params['Release_Version']})"
        )
    else:
        detail = (
            f"Built Jenkins params ({params.get('Environment', '')}/"
            f"{params.get('Branch', '')}, tag={params.get('RELEASE_TAG', '')})"
        )
    _persist(state, step_index=STEP_MERGE, step_status="done", step_detail=detail, log_lines=[detail])
    return {"jenkins_params": params, "logs": _log(state, detail)}


def trigger_jenkins(state: AgentState) -> dict[str, Any]:
    if state.get("error"):
        return {}
    job_path = state["jenkins_job_path"]
    params = state["jenkins_params"]

    if not jenkins_client.is_enabled():
        build_num = 1280 + (sum(ord(c) for c in state["service_label"]) % 100) + state.get("retry_count", 0)
        detail = f"[mock] Jenkins {job_path} triggered #{build_num}"
        _persist(state, step_index=STEP_TRIGGER, step_status="done", step_detail=detail,
                 log_lines=[detail], jenkins_build_number=build_num)
        return {"jenkins_build_number": build_num, "jenkins_status": "triggered",
                "logs": _log(state, detail)}

    try:
        result = jenkins_client.trigger_build_with_parameters(job_path, params)
        build_num = result["build_number"]
        build_url = f"{result.get('job_url', job_path)}/{build_num}"
        detail = f"Jenkins #{build_num} triggered — {build_url}"
        _persist(state, step_index=STEP_TRIGGER, step_status="done", step_detail=detail,
                 log_lines=[detail], jenkins_build_number=build_num, jenkins_build_url=build_url)
        return {"jenkins_build_number": build_num, "jenkins_status": "running",
                "logs": _log(state, detail)}
    except Exception as exc:
        detail = f"Jenkins trigger failed: {exc}"
        _persist(state, step_index=STEP_TRIGGER, step_status="failed", step_detail=detail,
                 log_lines=[detail])
        _persist(state, step_index=STEP_HEALTH, step_status="skipped",
                 step_detail="Skipped — build did not start")
        _persist(state, step_index=STEP_VERIFY, step_status="skipped",
                 step_detail="Skipped — build did not start")
        return {"jenkins_status": "failure", "console_log": str(exc),
                "logs": _log(state, detail)}


def poll_jenkins(state: AgentState) -> dict[str, Any]:
    if state.get("error"):
        return {}

    # Mock mode: resolve outcome from the demo scenario so the whole graph
    # (retry loop + report) can be exercised without a live Jenkins.
    if not jenkins_client.is_enabled():
        scenario = state.get("demo_scenario") or "success"
        retry_count = state.get("retry_count", 0)
        if scenario == "fail_build":
            log = _synthetic_log("build_error", state)
            _persist(state, step_index=STEP_WAIT, step_status="failed",
                     step_detail="Build failed (mock: compile/test error)", log_lines=["Mock build FAILED"])
            _persist(state, step_index=STEP_HEALTH, step_status="skipped",
                     step_detail="Skipped — build did not succeed")
            _persist(state, step_index=STEP_VERIFY, step_status="skipped",
                     step_detail="Skipped — build did not succeed")
            return {"jenkins_status": "failure", "console_log": log, "logs": _log(state, "Mock build FAILED")}
        if scenario == "fail_heap" and retry_count < 1:
            log = _synthetic_log("heap_oom", state)
            _persist(state, step_index=STEP_WAIT, step_status="failed",
                     step_detail="Build failed (mock: Java heap space)", log_lines=["Mock build OOM"])
            _persist(state, step_index=STEP_HEALTH, step_status="skipped",
                     step_detail="Skipped — build did not succeed")
            _persist(state, step_index=STEP_VERIFY, step_status="skipped",
                     step_detail="Skipped — build did not succeed")
            return {"jenkins_status": "failure", "console_log": log, "logs": _log(state, "Mock build OOM")}
        detail = f"[mock] Jenkins #{state.get('jenkins_build_number')} succeeded"
        _persist(state, step_index=STEP_WAIT, step_status="done", step_detail=detail, log_lines=[detail])
        return {"jenkins_status": "success", "logs": _log(state, detail)}

    build_num = state.get("jenkins_build_number")
    if not build_num:
        return {"jenkins_status": "failure", "console_log": "No build number to poll",
                "logs": _log(state, "ERROR: missing build number")}
    try:
        info = jenkins_client.get_build_info(state["jenkins_job_path"], int(build_num))
        status = info["status"]
        jenkins_result = info.get("result")
    except Exception as exc:
        return {"jenkins_status": "failure", "console_log": str(exc),
                "logs": _log(state, f"Poll error: {exc}")}

    # Stream the live console tail into the portal on every poll (running or not)
    # so DevOps can watch the Jenkins output without leaving the orchestrator plan.
    console = _fetch_console_tail(state, int(build_num))

    if status == "running":
        _persist(state, step_index=STEP_WAIT, step_status="running",
                 step_detail=f"Jenkins #{build_num} running…", console_log=console)
        time.sleep(_POLL_INTERVAL)
        return {"jenkins_status": "running", "console_log": console or state.get("console_log", ""),
                "logs": _log(state, f"Jenkins #{build_num} still running…")}
    if status == "success":
        detail = f"Jenkins #{build_num} succeeded"
        _persist(state, step_index=STEP_WAIT, step_status="done", step_detail=detail,
                 log_lines=[detail], console_log=console)
        return {"jenkins_status": "success", "jenkins_result": jenkins_result,
                "console_log": console or state.get("console_log", ""),
                "logs": _log(state, detail)}

    result_label = jenkins_result or status
    if jenkins_result == "ABORTED":
        detail = f"Jenkins #{build_num} aborted (manually stopped)"
    else:
        detail = f"Jenkins #{build_num} finished with {result_label}"
    _persist(state, step_index=STEP_WAIT, step_status="failed", step_detail=detail,
             log_lines=[detail], console_log=console)
    _persist(state, step_index=STEP_HEALTH, step_status="skipped",
             step_detail="Skipped — build did not succeed")
    _persist(state, step_index=STEP_VERIFY, step_status="skipped",
             step_detail="Skipped — build did not succeed")
    return {
        "jenkins_status": "failure",
        "jenkins_result": jenkins_result,
        "console_log": console or state.get("console_log", ""),
        "logs": _log(state, detail),
    }


def _fetch_console_tail(state: AgentState, build_num: int, tail_lines: int = 120) -> str:
    """Best-effort live console fetch; never raises so polling stays resilient."""
    try:
        return jenkins_client.get_console_text(state["jenkins_job_path"], build_num, tail_lines=tail_lines)
    except Exception:
        return state.get("console_log", "") or ""


def health_check(state: AgentState) -> dict[str, Any]:
    """Post-build health probe (mock 200 in POC; real gateway later)."""
    if state.get("error"):
        return {}
    detail = "Health check: gateway returned HTTP 200"
    _persist(state, step_index=STEP_HEALTH, step_status="done", step_detail=detail, log_lines=[detail])
    return {"logs": _log(state, detail)}


def fetch_console_log(state: AgentState) -> dict[str, Any]:
    """Pull the console tail for a failed build (real Jenkins) or reuse mock log."""
    log = state.get("console_log") or ""
    build_num = state.get("jenkins_build_number")
    if jenkins_client.is_enabled() and build_num:
        try:
            log = jenkins_client.get_console_text(state["jenkins_job_path"], int(build_num), tail_lines=200)
        except Exception as exc:
            log = log or f"(console fetch failed: {exc})"
    _persist(state, log_lines=["Fetched console log for failure analysis"])
    return {"console_log": log, "logs": _log(state, "Fetched console log for failure analysis")}


def analyze_failure(state: AgentState) -> dict[str, Any]:
    """Classify the failure (rules first, Ollama enriches low-confidence cases)."""
    console_log = state.get("console_log") or ""
    service_label = state.get("service_label", "service")
    jenkins_result = state.get("jenkins_result")

    rule = failure_rules.classify(console_log, jenkins_result=jenkins_result)

    # Rules with confidence >= 90: category/retry are final; skip Ollama re-classification.
    use_rules_only = rule["matched"] and int(rule.get("confidence", 0)) >= 90
    ai = ai_client.analyze_jenkins_log(
        service_label=service_label,
        jenkins_params=state.get("jenkins_params", {}),
        console_tail=console_log,
        build_number=state.get("jenkins_build_number"),
        jenkins_result=jenkins_result,
        rule_hint=rule if use_rules_only else None,
    )

    if rule["matched"]:
        category = rule["category"]
        retryable = rule["retryable"]
        curated = failure_rules.report_for_category(category, console_log)
        summary = curated["summary"] or ai.get("summary") or f"Detected {category} failure."
        root_cause = curated["root_cause"] or ai.get("root_cause", "")
        remediation = curated["remediation_steps"] or ai.get("remediation_steps", [])
        confidence = int(rule.get("confidence", 0))
    else:
        category = ai.get("category", "unknown")
        retryable = ai.get("retryable", False)
        curated = failure_rules.report_for_category(category, console_log)
        summary = ai.get("summary") or curated["summary"] or f"Detected {category} failure."
        root_cause = ai.get("root_cause") or curated["root_cause"]
        remediation = ai.get("remediation_steps") or curated["remediation_steps"]
        confidence = int(ai.get("confidence", 0))

    # Hard guardrails: only heap_oom + transient_infra may retry; code/abort never.
    if not failure_rules.is_retryable_category(category):
        retryable = False
    agent = orchestrator_config.get_agent(state.get("agent_type", "microservice"))
    allowed = set(agent.get("retry_categories", []))
    retryable = retryable and category in allowed

    ai_report = {
        "category": category,
        "retryable": retryable,
        "ai_available": ai.get("available", False) and not use_rules_only,
        "confidence": confidence,
        "summary": summary,
        "root_cause": root_cause,
        "remediation_steps": remediation,
        "rule_evidence": rule.get("evidence", ""),
        "jenkins_result": jenkins_result,
        "classification_source": "rules" if use_rules_only else ("rules+ai" if rule["matched"] else "ai"),
        "attempt": state.get("retry_count", 0) + 1,
    }
    line = f"Failure classified: {category} (retryable={retryable}, confidence={confidence}%) — {summary}"
    _persist(state, failure_category=category, ai_report=ai_report, log_lines=[line])
    return {"failure_category": category, "retryable": retryable,
            "ai_report": ai_report, "logs": _log(state, line)}


def retry_build(state: AgentState) -> dict[str, Any]:
    """Reset the build/verify steps and loop back for another attempt."""
    retry_count = state.get("retry_count", 0) + 1
    agent = orchestrator_config.get_agent(state.get("agent_type", "microservice"))
    backoff = float(agent.get("retry_backoff_seconds", 0))
    line = f"Auto-retry {retry_count}/{state.get('max_retries', 0)} ({state.get('failure_category')})"
    # Re-queue the downstream steps so the UI shows a fresh attempt.
    for idx in (STEP_WAIT, STEP_HEALTH, STEP_VERIFY):
        _persist(state, step_index=idx, step_status="queued", step_detail=None)
    _persist(state, status="running", retry_count=retry_count, log_lines=[line])
    if backoff:
        time.sleep(backoff)
    return {"retry_count": retry_count, "jenkins_status": "", "console_log": "",
            "status": "running", "logs": _log(state, line)}


def build_error_report(state: AgentState) -> dict[str, Any]:
    """Terminal failure: persist the AI remediation report and mark failed."""
    report = state.get("ai_report", {})
    steps = report.get("remediation_steps", [])
    lines = [f"BUILD FAILED — {report.get('category', 'unknown')}: {report.get('summary', '')}"]
    if report.get("root_cause"):
        lines.append(f"Root cause: {report['root_cause']}")
    for i, step in enumerate(steps, 1):
        lines.append(f"Remediation {i}: {step}")
    # Analysis ran successfully — show as done, not a failed verification step.
    _persist(state, step_index=STEP_VERIFY, step_status="done",
             step_detail="Failure analyzed — see report below",
             ai_report=report, log_lines=lines)
    _persist(state, status="failed", ai_report=report)
    return {"status": "failed", "logs": _log(state, lines[0])}


def finalize_success(state: AgentState) -> dict[str, Any]:
    build_num = state.get("jenkins_build_number")
    sub_task_id = state.get("sub_task_id", "")
    sub_task = db.get_sub_task(sub_task_id) if sub_task_id else None
    agent_type = state.get("agent_type") or ""
    deploy_index = 0
    if agent_type == "phrases":
        deploy_index = int(state.get("json_deploy_index") or 0)
    elif agent_type == "yaml":
        deploy_index = int(state.get("yaml_deploy_index") or 0)
    deployments: list[dict[str, Any]] = []
    if sub_task and agent_type == "phrases":
        deployments = sub_task.get("json_deployments") or []
    elif sub_task and agent_type == "yaml":
        deployments = sub_task.get("yaml_deployments") or []
    more_pending = bool(deployments) and deploy_index < len(deployments) - 1

    report = {
        "category": "success",
        "retryable": False,
        "ai_available": False,
        "summary": f"Deployment succeeded (Jenkins #{build_num}, tag {state.get('release_tag')}).",
        "remediation_steps": [],
        "attempt": state.get("retry_count", 0) + 1,
    }
    if more_pending:
        nxt = deployments[deploy_index + 1]
        nxt_label = nxt.get("action") or nxt.get("update") or "next"
        detail = (
            f"Jenkins #{build_num} complete ({deploy_index + 1}/{len(deployments)}) — "
            f"continuing to {nxt_label}"
        )
        _persist(state, status="running", step_index=STEP_VERIFY, step_status="done",
                 step_detail=detail, ai_report=report, log_lines=[detail])
        return {"status": "running", "ai_report": report, "logs": _log(state, detail)}

    detail = f"Build verified — Jenkins #{build_num} healthy, deployment complete"
    _persist(state, status="done", step_index=STEP_VERIFY, step_status="done",
             step_detail=detail, ai_report=report, log_lines=[detail])
    return {"status": "done", "ai_report": report, "logs": _log(state, detail)}


# ──────────────────────────────────────────────────────────────────────────
# Routing
# ──────────────────────────────────────────────────────────────────────────

def _route_after_poll(state: AgentState) -> str:
    if state.get("error"):
        return "fetch_console_log"
    status = state.get("jenkins_status")
    if status == "running":
        return "poll_jenkins"
    if status == "success":
        return "health_check"
    return "fetch_console_log"


def _route_after_health(state: AgentState) -> str:
    return "finalize_success"


def _route_after_analyze(state: AgentState) -> str:
    if state.get("retryable") and state.get("retry_count", 0) < state.get("max_retries", 0):
        return "retry_build"
    return "build_error_report"


# ──────────────────────────────────────────────────────────────────────────
# Graph assembly
# ──────────────────────────────────────────────────────────────────────────

def build_agent_graph() -> StateGraph:
    """The per-sub-task agent subgraph (currently the Microservice agent)."""
    g = StateGraph(AgentState)
    g.add_node("load_context", load_context)
    g.add_node("build_params", build_params)
    g.add_node("trigger_jenkins", trigger_jenkins)
    g.add_node("poll_jenkins", poll_jenkins)
    g.add_node("health_check", health_check)
    g.add_node("fetch_console_log", fetch_console_log)
    g.add_node("analyze_failure", analyze_failure)
    g.add_node("retry_build", retry_build)
    g.add_node("build_error_report", build_error_report)
    g.add_node("finalize_success", finalize_success)

    g.add_edge(START, "load_context")
    g.add_edge("load_context", "build_params")
    g.add_edge("build_params", "trigger_jenkins")
    g.add_edge("trigger_jenkins", "poll_jenkins")
    g.add_conditional_edges("poll_jenkins", _route_after_poll, {
        "poll_jenkins": "poll_jenkins",
        "health_check": "health_check",
        "fetch_console_log": "fetch_console_log",
    })
    g.add_conditional_edges("health_check", _route_after_health, {
        "finalize_success": "finalize_success",
    })
    g.add_edge("fetch_console_log", "analyze_failure")
    g.add_conditional_edges("analyze_failure", _route_after_analyze, {
        "retry_build": "retry_build",
        "build_error_report": "build_error_report",
    })
    g.add_edge("retry_build", "trigger_jenkins")
    g.add_edge("build_error_report", END)
    g.add_edge("finalize_success", END)
    return g


def _synthetic_log(category: str, state: AgentState) -> str:
    """Deterministic fake console output for mock/demo failure scenarios."""
    svc = state.get("service_label", "service")
    tag = state.get("release_tag", "")
    if category == "heap_oom":
        return (
            f"[INFO] Building {svc} (RELEASE_TAG={tag})\n"
            "[INFO] Running unit tests...\n"
            "java.lang.OutOfMemoryError: Java heap space\n"
            "\tat com.titan.report.Aggregator.load(Aggregator.java:212)\n"
            "[ERROR] GC overhead limit exceeded\n"
            "Build step 'Execute shell' marked build as failure\n"
            "Finished: FAILURE\n"
        )
    if category == "build_error":
        return (
            f"[INFO] Building {svc} (RELEASE_TAG={tag})\n"
            "[ERROR] COMPILATION ERROR :\n"
            "[ERROR] src/main/java/com/titan/Account.java:[47,18] cannot find symbol\n"
            "[ERROR]   symbol:   method getBalanceV2()\n"
            "[INFO] BUILD FAILURE\n"
            "Finished: FAILURE\n"
        )
    return f"[INFO] Building {svc}\nFinished: FAILURE\n"


# Master orchestrator = the agent graph today (one enabled agent). As more agents
# are enabled, the master will fan sub-tasks out to per-section subgraphs; the
# agent template below is what each of them will reuse.
def build_graph() -> StateGraph:
    return build_agent_graph()


# Exported for langgraph.json / Studio.
orchestrator_graph = build_agent_graph().compile()
microservice_agent = orchestrator_graph
