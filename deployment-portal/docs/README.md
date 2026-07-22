# Deployment Portal — Documentation

| Document | Description |
|----------|-------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System design, API, data model, approval flow |
| [OrcProgress.md](OrcProgress.md) | Living LangGraph orchestrator diagrams, retry policy, file map |
| [IMPLEMENTATION_V3.md](IMPLEMENTATION_V3.md) | Current v3 feature set (validation, approvals, sections) |
| [IMPLEMENTATION_V2.md](IMPLEMENTATION_V2.md) | v2 history (home dashboard, link-based requests) |

## Code layout (backend)

| Folder | Purpose |
|--------|---------|
| `backend/orchestrator/` | LangGraph agent graph, Jenkins client, mock executor, failure rules |
| `backend/validators/` | Pre-merge validation (GitSpace, YAML, Liquibase DB checks) |
| `backend/scripts/` | CLI utilities (`trigger_build.py`, `run_validation.py`, catalog tools) |
| `backend/config/` | JSON configuration (services, Jenkins jobs, orchestrator agents) |
