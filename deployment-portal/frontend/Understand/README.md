# Frontend — Understanding Guides

The React UI drives request creation, validation display, approvals, and live orchestrator status. Detailed **backend** call chains live under:

`deployment-portal/backend/Understand/`

| Backend doc | What the UI shows |
|-------------|-------------------|
| [01–07 validation](../../backend/Understand/README.md) | Validation panel / blocking errors before submit |
| [02–08 orchestrators](../../backend/Understand/README.md) | `OrchestratorPlan.jsx` steps, console, AI failure report |
| [09 master](../../backend/Understand/09-master-orchestrator.md) | Phases, queued builds, task status |

## Main UI touchpoints (quick)

| Area | Typical files |
|------|----------------|
| New request | `src/pages/NewRequest.jsx` |
| Task detail / validate / approve | `src/pages/TaskDetail.jsx`, `src/components/ApprovalPanel.jsx` |
| Orchestrator live plan | `src/components/OrchestratorPlan.jsx` |
| API client | `src/api/` (or equivalent fetch helpers) |

Frontend polls `GET /tasks/{id}/full` for validation report + sub-task steps/console while orchestration runs.

*Frontend-specific deep dives can be added here later; backend Understand is the source of truth for flows.*
