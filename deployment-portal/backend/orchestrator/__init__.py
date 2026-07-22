"""
orchestrator — LangGraph deployment orchestrator, Jenkins integration, and mock executor.

Public entry points used by main.py / db.py:
  - runner: background graph execution after DevOps approval
  - mock_executor: legacy tick-based simulation (ORCHESTRATOR_MODE=mock)
  - jenkins_params: Titan-Microservices parameter builder
"""

from orchestrator.runner import is_langgraph_mode, orchestrator_mode, run_task, run_task_async

__all__ = [
    "is_langgraph_mode",
    "orchestrator_mode",
    "run_task",
    "run_task_async",
]
