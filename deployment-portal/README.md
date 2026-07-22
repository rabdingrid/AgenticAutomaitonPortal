# Deployment Portal — v3

> v3 adds: dropdown-based Portal/Microservice selection, branch autocomplete
> (mocked GitSpace), a Phrases section, Release Branch per section, a
> Validate-before-submit gate, a sequential multi-stage approval chain
> (Dev Lead → [QA if code freeze] → DevOps) with rejection + comments, and a
> DevOps-only Code Freeze toggle on the Home page.

See [docs/IMPLEMENTATION_V3.md](docs/IMPLEMENTATION_V3.md) for the full v3 detail.

---

# Deployment Portal — v2 (history)

A full-stack deployment automation portal with a **home dashboard**, structured
request form (paste Gitspace links), and task/job orchestration tracking.

```
deployment-portal/
├── docs/                      Architecture, implementation notes, orchestrator progress
│   ├── ARCHITECTURE.md
│   ├── OrcProgress.md         Living LangGraph orchestrator diagrams
│   ├── IMPLEMENTATION_V2.md
│   └── IMPLEMENTATION_V3.md
├── backend/
│   ├── main.py                FastAPI entry point
│   ├── db.py                  Task/job persistence
│   ├── orchestrator/          LangGraph agent, Jenkins, mock executor
│   │   ├── graph.py           Agent subgraph (LangGraph Studio export)
│   │   ├── runner.py          Background orchestrator after approval
│   │   ├── jenkins_client.py  Trigger / poll / console
│   │   └── mock_executor.py   Legacy tick-based simulation
│   ├── validators/            Pre-merge validation pipeline
│   │   ├── validation.py      GitSpace + YAML + DB + build checks
│   │   └── liquibase_validator.py
│   ├── scripts/               CLI helpers (trigger build, validation, catalog)
│   ├── config/                JSON catalogs (services, Jenkins, agents)
│   └── requirements.txt
└── frontend/
    └── src/                   React portal UI
```

---

## Run locally

**Backend** (port 9002):

```bash
cd deployment-portal/backend
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 9002
```

**Frontend** (port 5173):

```bash
cd deployment-portal/frontend
npm install
npm run dev
```

Open `http://localhost:5173` — the app lands on the **Home** page.

---

## What's new in v2

| Feature | Description |
|---|---|
| **Home page** | Hero CTA, stats grid, quick actions, recent activity feed |
| **Link-paste form** | No GitLab API — users paste merge URLs with label + sub-type |
| **Three sections** | Build (MS/Portal/Utility), YAML (MS/Portal), DB (MS/Utility) |
| **+ Add links** | Multiple links per section with type selector per row |
| **Approver dropdown** | Loaded from `backend/config/approvers.json` |
| **Environments** | Dev, Support, Integ, UAT from `environments.json` |

---

## Request form fields

1. **Environment to promote** — dropdown (Dev, Support, Integ, UAT)
2. **Jira ID** — free text
3. **Description** — textarea
4. **Gitspace branch From / To** — free text (API fetch later)
5. **Build section** — toggle + microservice/portal/utility links
6. **YAML section** — toggle + microservice/portal links (no utility)
7. **DB section** — toggle + microservice/utility links (no portal)
8. **Approver** — dropdown from local config

---

## Edit approvers / environments

Edit these files directly (no restart needed — 2s cache TTL):

- `backend/config/approvers.json`
- `backend/config/environments.json`

---

## Demo data

On the Home or History page, click **Load demo data** / **Seed demo data** to
create 3 sample tasks (running, resolved, blocked).

---

## API endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/catalog/environments` | Environment list |
| GET | `/catalog/approvers` | Approver list |
| POST | `/tasks` | Create request |
| GET | `/tasks` | List tasks |
| GET | `/tasks/{id}` | Task detail |
| GET | `/activity` | Recent activity (home feed) |
| GET | `/stats` | Dashboard stats |
| POST | `/demo/seed` | Seed demo data |
