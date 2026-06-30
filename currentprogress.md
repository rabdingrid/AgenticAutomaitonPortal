# Deployment Portal — Current Progress

**Date:** June 29, 2026  
**Project:** AI Automation Portal / Deployment Portal v2  
**Location:** `deployment-portal/`

---

## Summary

Today we moved the deployment portal from a basic MVP to a **v2 simplified flow**: no GitLab/Gitspace API integration yet — users paste merge links directly. We added a **home dashboard**, rebuilt the **request form**, implemented a **dual-approval gate** before orchestration, and improved **history** to show full request details.

---

## Completed Today

### 1. Architecture simplification (v2)

- Removed dependency on live Gitspace/repo lookups for now.
- Users copy-paste Gitspace merge URLs with a label per link.
- Config driven by local JSON files (`catalog.py`) — ready to swap for S3 later.

### 2. Home page (`/`)

- Hero banner with primary CTA: **New deployment request**
- Stats grid: total, pending approval, resolved, in progress
- Quick actions: New request, View history, Load demo data
- Recent activity feed (last 6 tasks)

### 3. Request form (`/request`)

| Field | Implementation |
|---|---|
| Environment to promote | Dropdown — Dev, Support, Integ, UAT |
| Jira ID | Free text |
| Description | Textarea |
| Gitspace branch From / To | Free text (API fetch planned later) |
| Build section | Toggle + Microservice / Portal / Utilities links + **+** add more |
| YAML section | Toggle + Microservice / Portal only |
| DB section | Toggle + **Microservice only** (utility removed per latest spec) |
| Approver | Dropdown from `backend/config/approvers.json` |
| Orchestrator preview | Shows YAML → DB → Build execution order |

### 4. Dual approval workflow

- New requests start as **`pending_approval`** — orchestrator does **not** run immediately.
- Two approval buttons required before jobs start:
  - **Blue** — Approve as selected approver (name from form)
  - **Green** — Approve as DevOps team
- After **both** approve → first job moves to `running`, task status → `running`.
- API: `POST /tasks/{task_id}/approve` with `{ "role": "approver" | "devops" }`

### 5. History & task detail improvements

- **History** — expandable rows (▼) show all filled fields per request:
  - Environment, Jira ID, description, branches, approver, all section links
- Approval buttons visible inline for pending requests in History
- **Task detail** — full `RequestDetails` card + approval panel when pending
- Orchestrator job sequence only shown after both approvals

### 6. Backend (v2)

| File | Purpose |
|---|---|
| `catalog.py` | Loads environments + approvers from `backend/config/` |
| `db.py` | Tasks with sections (build/yaml/db), each job has `links[]` |
| `main.py` | API v0.2.0 — catalog, tasks, activity, stats, approve, demo seed |

**Job orchestration order:** YAML → DB → Build

### 7. Frontend components

| Component / Page | Role |
|---|---|
| `Home.jsx` | Dashboard landing page |
| `NewRequest.jsx` | Rebuilt multi-section form |
| `History.jsx` | Expandable list + approval actions |
| `TaskDetail.jsx` | Full details + approval + orchestrator |
| `LinkSectionEditor.jsx` | Reusable link rows with type selector + add/remove |
| `RequestDetails.jsx` | Shared full-field display for History & Task detail |

### 8. Documentation & deliverables

- `deployment-portal/README.md` — run instructions
- `deployment-portal/IMPLEMENTATION_V2.md` — v2 spec summary
- `deployment-portal-v2.zip` — packaged snapshot (project root)

---

## Current Data Model

**Task fields:**
- `environment`, `jira_id`, `description`, `branch_from`, `branch_to`
- `approver_key`, `requested_by`
- `status`: `pending_approval` | `running` | `done` | `blocked` | `failed`
- `approver_approved`, `devops_approved` (+ timestamps)

**Job (per section):**
- `section`: `build` | `yaml` | `db`
- `links[]`: `{ sub_type, url, label }`
- `status`, `steps`, `logs`, `agent`

**Allowed sub-types per section:**

| Section | Allowed types |
|---|---|
| Build | microservice, portal, utility |
| YAML | microservice, portal |
| DB | microservice only |

---

## How to Run

```bash
# Backend (port 9002)
cd deployment-portal/backend
pip install -r requirements.txt
uvicorn main:app --reload --port 9002

# Frontend (port 5173)
cd deployment-portal/frontend
npm install && npm run dev
```

Open `http://localhost:5173`

---

## Test Flow (end-to-end)

1. Open **Home** → click **New deployment request**
2. Fill form, enable sections, paste links, select approver → Submit
3. Task lands on detail page as **Pending approval**
4. Go to **History** → expand row (▼) → verify all fields visible
5. Click **blue** approver button, then **green** DevOps button
6. Orchestrator starts → YAML → DB → Build jobs run sequentially
7. Use **Seed demo data** on Home/History for sample tasks

---

## Not Yet Implemented (backlog)

| Item | Notes |
|---|---|
| Gitspace API integration | Branch/repo dropdowns — manual paste for now |
| Real auth (OIDC/Entra) | `requested_by` is placeholder `demo-user` |
| Real Jenkins webhooks | Simulate buttons stand in for build callbacks |
| Email notifications | On submit / approval / completion |
| S3 config backend | `catalog.py` interface ready, still local JSON |
| Production database | Still file-based `db.json` |
| Approval rejection flow | Approve only — no reject/deny button yet |

---

## File Tree (key files)

```
deployment-portal/
├── backend/
│   ├── main.py
│   ├── db.py
│   ├── catalog.py
│   ├── config/
│   │   ├── approvers.json
│   │   └── environments.json
│   └── requirements.txt
└── frontend/src/
    ├── App.jsx
    ├── api.js
    ├── styles.css
    ├── components/
    │   ├── LinkSectionEditor.jsx
    │   └── RequestDetails.jsx
    └── pages/
        ├── Home.jsx
        ├── NewRequest.jsx
        ├── History.jsx
        └── TaskDetail.jsx
```

---

## Status: Working prototype

The portal is a **functional full-stack prototype** suitable for demos and user feedback. Form submission, dual approval, orchestrator sequencing, and history/detail views are all wired end-to-end against a local JSON store.
