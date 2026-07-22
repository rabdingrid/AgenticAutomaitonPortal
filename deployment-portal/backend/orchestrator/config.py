"""
orchestrator_config.py — Load agent definitions + retry policy.

Backed by config/orchestrator_agents.json. Each agent maps a portal sub-task
type (microservice, portal, yaml, db, phrases) to its Jenkins job and a retry
policy. Microservice and Portal agents are enabled for the INTEG POC; yaml / db /
phrases are declared so the master orchestrator can grow one agent at a time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "orchestrator_agents.json"

_DEFAULT_AGENT = {
    "label": "Generic Agent",
    "jenkins_key": "",
    "enabled": False,
    "max_concurrent": 0,  # 0 = unlimited concurrency
    "max_retries": 0,
    "retry_backoff_seconds": 0,
    "retry_categories": [],
}


def _load() -> dict[str, Any]:
    with _CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def get_agent(agent_type: str) -> dict[str, Any]:
    """Return the agent config for a sub-task type, falling back to defaults."""
    cfg = _load().get(agent_type)
    if not cfg:
        return {**_DEFAULT_AGENT, "agent_type": agent_type}
    return {**_DEFAULT_AGENT, **cfg, "agent_type": agent_type}


def all_agents() -> dict[str, Any]:
    return _load()


def is_agent_enabled(agent_type: str) -> bool:
    return bool(get_agent(agent_type).get("enabled"))


def max_concurrent(agent_type: str) -> int:
    """Max builds of this agent type allowed to run at once. 0 = unlimited."""
    try:
        return int(get_agent(agent_type).get("max_concurrent", 0) or 0)
    except (TypeError, ValueError):
        return 0
