# Agentic Automation Portal — Project Overview

> **Purpose of this document:** Give any AI assistant or developer complete context about this codebase — what it is, how it is structured, how data flows, what is implemented today, and what is planned next.

---

## 1. What this project is

**Agentic Automation Portal** (repo: [rabdingrid/AgenticAutomaitonPortal](https://github.com/rabdingrid/AgenticAutomaitonPortal), branch: `dev_v1`) is an **Internal Developer Platform (IDP)** focused on **self-service deployment requests**.

It lets developers submit structured deployment requests by pasting **Gitspace merge URLs** (and related YAML/DB links) into a web form. Each request becomes a **Task** that is tracked through a **dual-approval gate** and then executed by an **orchestrator** as a sequence of **Jobs** (YAML → DB → Build).

This is a **lightweight, narrowly-scoped IDP** — not a full CI/CD platform. The industry term for this category is:

```
Service Catalog  →  Self-Service Form  →  Orchestrated Actions  →  Audit Trail
```

### Current maturity: **v2 MVP / demo**

| Area | Status |
|------|--------|
| Home dashboard, request form, history, task detail UI | ✅ Implemented |
| File-based JSON persistence (`db.json`) | ✅ Implemented |
| Dual approval workflow (approver + DevOps) | ✅ Implemented |
| Sequential job orchestration (YAML → DB → Build) | ✅ Implemented (in-process, simulated) |
| GitLab / Jenkins / Liquibase integration | ❌ Not yet — users paste URLs manually |
| RBAC / Entra ID auth | ❌ Not yet — hardcoded `demo-user` |
| Real agent workers (build_agent, yaml_automation_agent, liquibase_agent) | ❌ Not yet — demo simulate buttons only |
| S3-backed catalog config | ❌ Not yet — local JSON files (S3-ready interface) |
| Notification bus (Slack/email) | ❌ Not yet |

---

## 2. Repository layout

```
AI_AutomationPortal/                    ← repo root (this README)
├── README.md                           ← you are here
├── RESEARCH.md                         ← industry research (Backstage, Spinnaker, RBAC patterns)
├── implementationPlan.md               ← empty placeholder
├── implenPlanNew.md                      ← empty placeholder
├── deployment-portal-v2.zip              ← packaged snapshot of the app
└── deployment-portal/                    ← **main application** (all source code)
    ├── README.md                         ← shorter run guide
    ├── IMPLEMENTATION_V2.md              ← v2 feature summary
    ├── backend/
    │   ├── main.py                       ← FastAPI app, all HTTP routes
    │   ├── db.py                         ← file DB, task/job CRUD, orchestrator logic
    │   ├── catalog.py                    ← environments + approvers config loader
    │   ├── orchestrator.py               ← DAG rules (future/advanced; not wired into db.py v2)
    │   ├── run.sh                        ← one-command backend startup script
    │   ├── requirements.txt
    │   ├── db.json                       ← runtime data (auto-created)
    │   └── config/
    │       ├── environments.json
    │       └── approvers.json
    └── frontend/
        ├── package.json
        ├── vite.config.js                ← proxies /api → localhost:9002
        └── src/
            ├── App.jsx                   ← shell + React Router
            ├── api.js                    ← fetch wrapper for all backend calls
            ├── styles.css
            ├── components/
            │   ├── LinkSectionEditor.jsx ← toggleable section with link rows
            │   └── RequestDetails.jsx    ← read-only task detail table
            └── pages/
                ├── Home.jsx              ← dashboard
                ├── NewRequest.jsx        ← request form
                ├── TaskDetail.jsx        ← task + job stepper + approval
                └── History.jsx           ← full task list
```

---

## 3. Tech stack

| Layer | Technology |
|-------|------------|
| Frontend | React 18, React Router 6, Vite 5 |
| Backend | Python 3.10+ (prefer **3.13**), FastAPI 0.115, Pydantic 2.9, Uvicorn 0.30 |
| Persistence | Local JSON file (`backend/db.json`) with thread lock |
| Config | Local JSON files (`backend/config/*.json`) with 2s cache TTL |
| Auth | None (MVP) — `requested_by` is hardcoded to `"demo-user"` |
| External integrations | None yet |

### Important: Python version

The backend uses modern type syntax (`str | None`, `list[LinkInput]`). **Python 3.10+ is required.** The bundled `run.sh` defaults to `python3.13`. Creating a venv with system Python 3.9 will crash at startup.

---

## 4. Core domain model

### Task

A deployment request submitted by a user. One task can include multiple sections (Build, YAML, DB).

**Lifecycle statuses:** `pending_approval` → `running` → `done` | `blocked`

| Field | Description |
|-------|-------------|
| `task_id` | Auto-generated, e.g. `TASK-20392` |
| `environment` | Target env key: `DEV`, `SUPPORT`, `INTEG`, `UAT` |
| `jira_id` | Jira ticket reference |
| `description` | Free text |
| `branch_from` / `branch_to` | Gitspace branch names (informational for now) |
| `approver_key` | Key from `approvers.json` |
| `requested_by` | User identity (hardcoded `demo-user` in MVP) |
| `approver_approved` / `devops_approved` | Boolean approval flags |
| `jobs` | Ordered list of job IDs |

### Job

One job per enabled section in a task. Jobs run **sequentially** in this order:

```
YAML (order 1) → DB (order 2) → Build (order 3)
```

**Job statuses:** `queued` → `running` → `done` | `failed`

| Field | Description |
|-------|-------------|
| `job_id` | Auto-generated, e.g. `JOB-1` |
| `section` | `yaml`, `db`, or `build` |
| `agent` | Target worker name (not connected yet) |
| `links` | Array of `{sub_type, url, label}` pasted by user |
| `steps` | 4-step progress template per section type |
| `logs` | Timestamped log lines |

**Agent mapping (planned workers):**

| Section | Agent name |
|---------|------------|
| `build` | `build_agent` |
| `yaml` | `yaml_automation_agent` |
| `db` | `liquibase_agent` |

### Section link rules

| Section | Allowed `sub_type` values |
|---------|---------------------------|
| `build` | `microservice`, `portal`, `utility` |
| `yaml` | `microservice`, `portal` |
| `db` | `microservice` only |

Each section must have at least one link with a non-empty URL.

### Approval workflow

1. User submits request → task status = `pending_approval`, all jobs = `queued`
2. **Both** must approve before orchestrator starts:
   - Designated **approver** (`role: "approver"`)
   - **DevOps team** (`role: "devops"`)
3. When both approve → first job (YAML if present) moves to `running`, task → `running`
4. When a job completes (`done`) → next queued job auto-starts
5. If any job fails → task → `blocked` (no automatic retry)

### Orchestrator (current implementation in `db.py`)

The live orchestrator is **simple sequential dispatch** inside `db.py`:

- `_start_orchestrator()` — kicks off the first job after dual approval
- `_advance_orchestrator()` — on job `done`/`failed`, starts next queued job or sets task `done`/`blocked`

`orchestrator.py` contains a more advanced **DAG-based** design (script → yaml → db → microservice → portal) for future use. It is **not currently imported** by `main.py` or `db.py`.

---

## 5. API reference

Base URL: `http://localhost:9002`  
Frontend proxy: `/api/*` → backend (configured in `vite.config.js`)

### Health & catalog

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | `{ "status": "ok" }` |
| `GET` | `/catalog/environments` | List promotion environments |
| `GET` | `/catalog/approvers` | List approvers |

### Tasks

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/tasks` | Create a new deployment request |
| `GET` | `/tasks` | List tasks (`?status=`, `?requested_by=` filters) |
| `GET` | `/tasks/{task_id}` | Task detail with expanded jobs |
| `POST` | `/tasks/{task_id}/approve` | Approve as `approver` or `devops` |

**Create task payload:**

```json
{
  "environment": "INTEG",
  "jira_id": "TRB-16996",
  "description": "Account service update",
  "branch_from": "develop",
  "branch_to": "release/2026-06",
  "approver_key": "a-sharma",
  "sections": [
    {
      "section": "yaml",
      "links": [
        { "sub_type": "microservice", "url": "https://gitspace/.../!2816", "label": "parameters.yml" }
      ]
    },
    {
      "section": "build",
      "links": [
        { "sub_type": "microservice", "url": "https://gitspace/.../!2814", "label": "AccountService" }
      ]
    }
  ]
}
```

### Jobs

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/jobs/{job_id}` | Single job detail |
| `PATCH` | `/jobs/{job_id}/status` | Update status + optional log line (demo control) |

### Dashboard & demo

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/stats?period=weekly` | Dashboard counts (`daily`/`weekly`/`monthly`) |
| `GET` | `/activity?limit=6` | Recent tasks for home feed |
| `POST` | `/demo/seed` | Reset DB and create 3 sample tasks |
| `POST` | `/demo/reset` | Clear all data |

---

## 6. Frontend routes & pages

| Route | Component | Purpose |
|-------|-----------|---------|
| `/` | `Home.jsx` | Hero, stats grid, quick actions, recent activity |
| `/request` | `NewRequest.jsx` | Deployment request form |
| `/tasks/:taskId` | `TaskDetail.jsx` | Approval UI, job stepper, simulate done/fail |
| `/history` | `History.jsx` | All tasks, expandable rows, seed/reset demo |

### Key UI behaviors

- **NewRequest** loads environments/approvers from catalog API on mount
- Sections are toggleable; each has dynamic link rows via `LinkSectionEditor`
- Orchestrator preview shows execution order before submit
- **TaskDetail** polls every 3 seconds for live status updates
- Running jobs expose **demo buttons** to manually mark done/failed (simulates agent completion)
- **History** supports inline approval without navigating to task detail

### API client (`frontend/src/api.js`)

All backend calls go through `api.*` helpers. Errors are parsed from FastAPI `detail` field.

---

## 7. Configuration files

Editable without server restart (2-second cache TTL in `catalog.py`):

**`backend/config/environments.json`**
```json
[
  { "key": "DEV", "label": "Dev", "order": 1 },
  { "key": "SUPPORT", "label": "Support", "order": 2 },
  { "key": "INTEG", "label": "Integ", "order": 3 },
  { "key": "UAT", "label": "UAT", "order": 4 }
]
```

**`backend/config/approvers.json`**
```json
[
  { "key": "a-sharma", "name": "A. Sharma", "email": "a.sharma@company.com" }
]
```

**`backend/db.json`** — runtime task/job store. Auto-created on first run. Reset via `/demo/reset` or deleting the file.

---

## 8. How to run locally

### Prerequisites

- **Python 3.10+** (3.13 recommended — `/opt/homebrew/bin/python3.13` on macOS)
- **Node.js 18+** and npm

### Backend (port 9002)

```bash
cd deployment-portal/backend
./run.sh
```

Or manually:

```bash
cd deployment-portal/backend
/opt/homebrew/bin/python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 9002
```

### Frontend (port 5173)

```bash
cd deployment-portal/frontend
npm install
npm run dev
```

Open **http://localhost:5173**

### Demo data

On Home or History, click **Load demo data** / **Seed demo data** — creates 3 sample tasks:
1. Multi-section task (YAML + DB + Build) — pending approval
2. Build-only portal task — fully resolved
3. DB-only UAT task — blocked/failed

---

## 9. Data flow (end-to-end)

```
User fills form (NewRequest.jsx)
        │
        ▼
POST /tasks  ──►  db.create_task()
        │              │
        │              ├─ validates section links
        │              ├─ sorts sections: yaml → db → build
        │              ├─ creates jobs (status: queued)
        │              └─ task status: pending_approval
        ▼
TaskDetail.jsx  ◄──  GET /tasks/{id}  (polls every 3s)
        │
        ├─ Approver clicks "Approve as {name}"  →  POST /tasks/{id}/approve {role:"approver"}
        ├─ DevOps clicks "Approve as DevOps"  →  POST /tasks/{id}/approve {role:"devops"}
        │
        ▼ (both approved)
db._start_orchestrator()  →  first job → running
        │
        ▼ (demo: user clicks "Simulate: mark done")
PATCH /jobs/{id}/status {status:"done"}
        │
        ▼
db._advance_orchestrator()  →  next job → running  (or task → done)
        │
        ▼ (if any job fails)
task → blocked
```

---

## 10. Planned / not-yet-built features

Derived from `RESEARCH.md`, code comments, and architectural intent:

| Feature | Notes |
|---------|-------|
| **GitLab API integration** | Fetch MR details from pasted URLs instead of manual entry |
| **Jenkins triggers** | `build_agent` calls Jenkins with structured params (not shell strings) |
| **Liquibase runner** | `liquibase_agent` executes DB migrations |
| **YAML config agent** | `yaml_automation_agent` applies config changes |
| **RBAC / Entra ID** | `require_env_access()` on every route; fail-closed by design |
| **S3 catalog backend** | `catalog.py` interface is S3-ready; currently local JSON |
| **Real worker pool** | Swap in-process orchestrator for Temporal/Prefect/custom workers |
| **Notification bus** | Slack/email on job failure (Spinnaker Echo pattern) |
| **Retry single job** | Currently whole task blocks on failure; no per-job retry |
| **Auth / real user identity** | Replace hardcoded `demo-user` |
| **DAG orchestrator** | `orchestrator.py` has parallel-branch rules; not wired yet |

### Architectural mapping (planned backend modules)

| Component | Role | File (planned) |
|-----------|------|----------------|
| API gateway | HTTP routes | `main.py` ✅ |
| Orchestrator | Pipeline dispatch | `db.py` ✅ (simple), `orchestrator.py` (advanced) |
| Authorization | Env access checks | `rbac.py` (planned) |
| GitLab client | MR merge/fetch | `gitlab_client.py` (planned) |
| Event bus | Notifications | Echo-equivalent (planned) |

---

## 11. Key design decisions

1. **Link-paste over GitLab picker (v2):** Users paste Gitspace merge URLs directly. No GitLab API dependency yet — faster MVP.
2. **Dual approval gate:** Both business approver and DevOps must sign off before any job runs — mirrors Spinnaker manual judgment stages.
3. **Sequential section ordering:** YAML config must land before DB migrations, which must land before builds.
4. **Fail-closed on job failure:** One failed job blocks the entire task (no backtracking) — same tradeoff Spinnaker accepts.
5. **File DB for MVP:** `db.json` with thread lock is sufficient for demo; designed to be swappable for a real DB later.
6. **Structured params over string templates:** Future Jenkins/GitLab calls will use dict assembly, not string interpolation (security lesson from Backstage/Nunjucks research).
7. **Backend authorization over UI hiding:** Future RBAC checks happen in route handlers, not just by hiding dropdown options.

---

## 12. Related documentation

| File | Contents |
|------|----------|
| `deployment-portal/README.md` | Quick start + API table |
| `deployment-portal/IMPLEMENTATION_V2.md` | v2 feature breakdown |
| `RESEARCH.md` | Industry comparison (Backstage, Spinnaker, Port, RBAC patterns) |
| `implementationPlan.md` | Empty — placeholder for future planning |
| `implenPlanNew.md` | Empty — placeholder for future planning |

---

## 13. Common tasks for AI assistants

### Add a new environment
Edit `backend/config/environments.json`, add `{ "key": "PROD", "label": "Production", "order": 5 }`.

### Add a new approver
Edit `backend/config/approvers.json`.

### Add a new API endpoint
1. Add route in `backend/main.py`
2. Add business logic in `backend/db.py` or new module
3. Add client method in `frontend/src/api.js`
4. Wire UI in relevant page component

### Change job execution order
Modify `SECTION_ORDER` in `backend/db.py` (currently `yaml: 1, db: 2, build: 3`).

### Change allowed link types per section
Modify `SECTION_ALLOWED_SUBTYPES` in `backend/db.py`.

### Debug orchestrator issues
- Check `backend/db.json` for task/job states
- Use `POST /demo/reset` to clear state
- Use `POST /demo/seed` to restore sample data
- On TaskDetail, use simulate done/fail buttons to step through jobs

---

## 14. Git info

- **Remote:** https://github.com/rabdingrid/AgenticAutomaitonPortal
- **Active branch:** `dev_v1`
- **Local workspace:** `/Users/shukumar/Desktop/AI_AutomationPortal`

---

*Last updated: June 2026 — reflects `dev_v1` branch state.*
