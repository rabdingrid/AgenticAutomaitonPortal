# Deployment Portal — v2

A full-stack deployment automation portal with a **home dashboard**, structured
request form (paste Gitspace links), and task/job orchestration tracking.

```
deployment-portal/
├── backend/
│   ├── main.py           API endpoints (v2)
│   ├── db.py             Task/job persistence (sections + links model)
│   ├── catalog.py        Local JSON config (environments, approvers)
│   ├── config/           Auto-created on first run
│   │   ├── environments.json
│   │   └── approvers.json
│   └── requirements.txt
└── frontend/
    └── src/
        ├── App.jsx
        ├── api.js
        ├── styles.css
        ├── components/
        │   └── LinkSectionEditor.jsx
        └── pages/
            ├── Home.jsx          Dashboard (stats + recent activity)
            ├── NewRequest.jsx    Request form with link paste sections
            ├── TaskDetail.jsx    Job stepper + link details
            └── History.jsx       Full task list
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
