# Deployment Portal v2 — Implementation Summary

## Overview

v2 simplifies the deployment request flow: no GitLab API integration yet. Users paste
Gitspace merge URLs directly into structured sections. The app opens on a **Home**
dashboard instead of the request form.

---

## Home Page (`/`)

| Section | Content |
|---|---|
| **Hero** | Welcome message + primary "New deployment request" CTA |
| **Stats grid** | Total, Resolved, In progress, Failed/blocked (daily/weekly/monthly toggle) |
| **Quick actions** | New request, View history, Load demo data |
| **Recent activity** | Last 6 tasks with status badges and section pills |

---

## Request Form (`/request`)

### Top fields
- Environment to promote (Dev, Support, Integ, UAT)
- Jira ID
- Description
- Gitspace branch From / To

### Three toggleable sections

| Section | Allowed sub-types | UI |
|---|---|---|
| **Build** (Gitspace merge) | Microservice, Portal, Utilities | Toggle on/off, type dropdown per row, label + URL, + to add more |
| **YAML** | Microservice, Portal only | Same pattern, no Utilities |
| **DB** | Microservice, Utilities only | Same pattern, no Portal |

### Approval
- Approver dropdown from `backend/config/approvers.json`

### Orchestrator preview
Shows execution order: YAML → DB → Build when multiple sections are enabled.

---

## Backend changes

| File | Role |
|---|---|
| `catalog.py` | Loads environments + approvers from local JSON (S3-ready interface) |
| `db.py` | New data model: tasks have `sections` as jobs with `links[]` |
| `main.py` | New endpoints: `/catalog/*`, `/activity`, updated `/tasks` payload |

### Job ordering
1. YAML
2. DB
3. Build

---

## Config files (editable)

**`backend/config/approvers.json`**
```json
[
  { "key": "a-sharma", "name": "A. Sharma", "email": "a.sharma@company.com" }
]
```

**`backend/config/environments.json`**
```json
[
  { "key": "DEV", "label": "Dev", "order": 1 },
  { "key": "SUPPORT", "label": "Support", "order": 2 },
  { "key": "INTEG", "label": "Integ", "order": 3 },
  { "key": "UAT", "label": "UAT", "order": 4 }
]
```

---

## Run

```bash
# Terminal 1 — backend
cd deployment-portal/backend
pip install -r requirements.txt
uvicorn main:app --reload --port 9002

# Terminal 2 — frontend
cd deployment-portal/frontend
npm install && npm run dev
```

Open `http://localhost:5173`

---

## Deliverables

- Source: `deployment-portal/` directory
- Zip: `deployment-portal-v2.zip` (project root)
- Docs: this file + `deployment-portal/README.md`
