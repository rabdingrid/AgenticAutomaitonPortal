# Deployment Portal v3 — Implementation Summary

Builds on v2. Adds branch autocomplete, a Phrases section, a validation gate,
a conditional multi-stage approval chain, and a Code Freeze workflow.

---

## 1. New Request page

| Field | Behavior |
|---|---|
| Environment to promote | Dropdown (Dev, Support, Integ, UAT) |
| Jira ID | Free text |
| Description | Textarea |
| Branch **From** | Autocomplete — fetches branches (mock GitSpace) for the first selected service, filters as you type |
| Branch **To** | Auto-populated (read-only) from the selected environment |
| Approver (Dev Lead) | Dropdown from `config/approvers.json` |

### Sections (toggle on/off, each with + to add more rows)

| Section | Sub-types | Release branch |
|---|---|---|
| YAML / Config | Microservice, Portal | Required |
| DB / Liquibase | Microservice, Portal | Required |
| **Phrases** (new) | Microservice, Portal | Required |
| Build (Gitspace merge) | Microservice, Portal, Utilities | Not used |

Portal/Microservice are now **dropdowns from the service catalog** (no pasted
URLs). Each link stores `service_key` instead of `url`.

> Note: the meeting discussion specified DB = "Portal and Microservice(s)"
> (same as YAML), so DB allows microservice + portal in v3.

### Validate gate
- **Validate** button calls `POST /tasks/validate` (same checks as submit).
- Submit is disabled until validation passes.
- Editing any field after a pass re-locks Submit (forces re-validation).
- Failed validations are shown as a clear checklist.

---

## 2. Approval workflow (sequential, conditional)

```
Code Freeze OFF:  Dev Lead  →  DevOps
Code Freeze ON:   Dev Lead  →  QA  →  DevOps
```

Rules (all tested):
1. Strictly sequential — a stage cannot act before the previous one approves.
2. Approving the **last** stage starts the orchestrator (first job → running).
3. Rejecting at **any** stage requires a comment and immediately sets `rejected`.
4. A rejected task is terminal; its reason is displayed.
5. The chain shape is **captured at creation** — toggling code freeze later does
   not change a task already mid-approval.
6. QA does not exist in the chain at all when code freeze is off.

Email notifications are **mocked** → logged to `backend/notifications.log` and
printed to the uvicorn console (submit, stage-approved, rejected).

---

## 3. Code Freeze (Home page)

- DevOps-only toggle (role gating stubbed: `IS_DEVOPS_ROLE = true` for now).
- Stored in `config/code_freeze.json`.
- Enabling it makes **new** requests use the 3-stage chain.

---

## 4. Backend

| File | Change |
|---|---|
| `catalog.py` | `load_services` / `get_service`, `load/save_code_freeze` |
| `mock_gitlab.py` (new) | `list_branches(project_path, query)` — deterministic, demo-reliable |
| `notifications.py` (new) | Mocked email → `notifications.log` |
| `db.py` | Approval state machine (`get_approval_chain`, `get_current_approval_stage`, `apply_approval_decision`, `ApprovalError`), `service_key` links, `release_branch`, `phrases` section |
| `main.py` | `/catalog/services`, `/catalog/branches`, `/catalog/code-freeze` (GET/PUT), `/tasks/validate`, `/tasks/{id}/approve` (409 out-of-turn, 422 missing comment) |

### Task shape additions
```
code_freeze_enabled: bool
approval_chain: ["dev_lead", "devops"]   # or [..., "qa", ...]
approvals: { "dev_lead": null | {decision, comment, by, at}, ... }
rejection_reason: str | null
current_stage: str | null   # computed on read
```

---

## 5. Frontend

| File | Change |
|---|---|
| `api.js` | services, branches, code-freeze, validate, approve calls |
| `components/BranchAutocomplete.jsx` (new) | Debounced branch search dropdown |
| `components/LinkSectionEditor.jsx` | Service dropdowns + release branch field |
| `components/ApprovalPanel.jsx` (new) | Sequential chain UI + reject-with-comment, shared by TaskDetail & History |
| `components/RequestDetails.jsx` | Shows services + release branch (no URLs) |
| `pages/NewRequest.jsx` | Phrases section, Validate gate, branch auto-fill |
| `pages/Home.jsx` | Code Freeze toggle |
| `pages/TaskDetail.jsx`, `History.jsx` | Use shared `ApprovalPanel`, rejected state |

---

## 6. Run

```bash
cd deployment-portal/backend
pip install -r requirements.txt
uvicorn main:app --reload --port 9002

cd deployment-portal/frontend
npm install && npm run dev
```

Open `http://localhost:5173`.

---

## 7. Verified (automated)

- 6 approval-chain scenarios (normal, freeze, reject-needs-comment, reject-halts,
  out-of-turn-blocked, QA-absent-when-off)
- Chain persists across a code-freeze toggle mid-approval
- Validate returns the same errors submission would reject
- Branch autocomplete returns non-empty results for common queries
- Full HTTP flow against the live server (validate → create → approve → running)

---

## 8. Still mocked / out of scope

- Real GitLab/GitSpace API (branches are mocked)
- Real email (logged, not sent)
- Real auth/RBAC (DevOps gating stubbed)
- Editing & resubmitting a rejected task (rejected is terminal — create a new request)
