# Deployment Portal — Architecture

This document describes the **current** system and the **AI validation** flow (including new-vs-old YAML/DB comparison).

---

## 1. System context

```mermaid
flowchart TB
    subgraph Users["Users"]
        Dev["Developer"]
        Lead["Dev Lead"]
        DevOps["DevOps"]
    end

    subgraph Frontend["Frontend — React :5173"]
        direction TB
        F1["Home / Stats"]
        F2["NewRequest"]
        F3["TaskDetail"]
        F4["ValidationReport"]
        F5["OrchestratorPlan"]
        F6["ApprovalPanel"]
    end

    subgraph Backend["Backend — FastAPI :9002"]
        direction TB
        API["main.py"]
        AUTH["auth.py"]
        CAT["catalog.py"]
        VAL["validation.py"]
        GS["gitspace.py"]
        AI["ai_client.py"]
        DBM["db.py"]
        ORCH["orchestrator.py"]
        EXEC["mock_executor.py"]
        MAIL["notifications.py + graph_mail.py"]
    end

    subgraph Config["Configuration"]
        C1["catalog_services.json"]
        C2["gitlab_repos.json"]
        C3["gitspace.json"]
        C4[".env"]
    end

    subgraph LocalAI["Local AI"]
        OLL["Ollama :11434<br/>qwen2.5-coder:7b"]
    end

    subgraph External["External systems"]
        GIT["GitSpace / GitLab<br/>flpi-titan-release + service repos"]
        AZ["Microsoft Graph<br/>email"]
        JEN["Jenkins — planned"]
    end

    Dev --> F2
    Lead --> F3
    DevOps --> F1

    F2 & F3 & F1 --> API
    API --> AUTH & CAT & VAL & DBM & ORCH & EXEC & MAIL
    VAL --> GS & AI
    GS --> GIT
    AI --> OLL
    MAIL --> AZ
    EXEC -.-> JEN

    CAT --> C1
    GS --> C2 & C3
    API --> C4
```

---

## 2. Request lifecycle

```mermaid
sequenceDiagram
    autonumber
    actor Dev as Developer
    participant UI as NewRequest UI
    participant API as FastAPI
    participant Val as validation.py
    participant GS as GitSpace client
    participant AI as ai_client / Ollama
    participant DB as db.json
    participant Lead as Dev Lead

    Dev->>UI: Fill sections + branches
    Dev->>API: POST /validate/preview
    API->>Val: run_validation(use_ai)
    Val->>GS: branch_exists, get_file, list_tree
    Val->>AI: compare_artifacts (YAML/DB new vs old)
    Val->>AI: analyze_artifact (content review)
    Val->>AI: summarize_report (briefing)
    AI-->>Val: verdicts + markdown
    Val-->>UI: ValidationReport

    Dev->>API: POST /tasks
    API->>DB: create_task + jobs + sub_tasks
    API->>Val: background validation
  API->>Lead: email notification

    Lead->>API: POST /tasks/{id}/approve
    API->>DB: advance approval chain
    API->>ORCH: dispatch sub-tasks
    loop Each sub-task
        UI->>API: POST /sub-tasks/{id}/tick
        API->>EXEC: mock_executor.tick_sub_task
    end
```

---

## 3. AI validation — YAML baseline diff + DB Liquibase pre-check

Artifacts live on the **release repo** (`Titan/flpi-titan-release`):

```text
Microservices/{Service}/{Release}/YML/parameters.yml
Microservices/{Service}/{Release}/DB/Common.sql
Microservices/{Service}/{Release}/DB/INTEG.sql   (INTEG only)
Microservices/{Service}/{Release}/DB/UAT.sql     (UAT only)
Microservices/{Service}/{Release}/DB/PROD.sql  (PROD only)
```

### YAML (baseline comparison)

For each YAML file on the **new** release (e.g. `R-2026-06-W4`):

1. Resolves a **baseline** (old) release folder — e.g. `R-2026-06-W3`
2. Fetches **old** and **new** file content from GitSpace
3. Runs **deterministic** diff checks (YAML key changes)
4. Runs **AI** `compare_artifacts()` when `VALIDATION_USE_AI=true`

### DB (no baseline — Liquibase pre-checker)

For each service, merge env-specific SQL and validate deployability:

| Environment | Files merged |
|-------------|----------------|
| INTEG | `Common.sql` + `INTEG.sql` |
| UAT | `Common.sql` + `UAT.sql` |
| PROD | `Common.sql` + `PROD.sql` |

Only changesets with `labels:Approved` (or `labels:approved`) are validated; `labels:Pending` are ignored.
Changeset headers must use `-- Changeset author:id` (space after `--`).

Deterministic checks (`liquibase_validator.py`): Liquibase header, changeset `author:id` format,
Approved labels, duplicate IDs within merged changelog, empty changesets, basic SQL syntax,
destructive SQL warnings. AI `review_db_changelog()` provides **insights only** (never blocks).

```mermaid
flowchart LR
    subgraph Input["Request"]
        RB["release_branch<br/>R-2026-06-W4"]
        SVC["service_key<br/>yaml:microservice:account"]
    end

    subgraph Resolve["gitspace.py"]
        PATH["section_dir_for()<br/>Microservices/Account/R-2026-06-W4/YML"]
        BASE["resolve_baseline_release()<br/>→ R-2026-06-W3"]
        OLD_PATH["swap_release_in_path()<br/>.../R-2026-06-W3/YML/..."]
    end

    subgraph Fetch["GitSpace client"]
        NEW_F["get_file(ref=W4, role=new)"]
        OLD_F["get_file(ref=W3, role=old)"]
    end

    subgraph Checks["validation.py"]
        DET["Deterministic diff<br/>changeset collision, YAML keys"]
        AI_CMP["ai_client.compare_artifacts()"]
        AI_REV["ai_client.analyze_artifact()"]
    end

    subgraph Output["Report item"]
        CHK["checks[]"]
        URL["urls.file + urls.baseline_file"]
        INS["ai_insight"]
    end

    RB --> PATH
    SVC --> PATH
    PATH --> BASE
    BASE --> OLD_PATH
    PATH --> NEW_F
    OLD_PATH --> OLD_F
    NEW_F --> DET
    OLD_F --> DET
    DET --> AI_CMP
    NEW_F --> AI_REV
    DET --> CHK
    AI_CMP --> CHK
    AI_REV --> INS
    CHK --> Output
```

### Baseline resolution order

| Priority | Source |
|----------|--------|
| 1 | Env `VALIDATION_BASELINE_RELEASE` (global override) |
| 2 | Sibling folders under `Microservices/{Service}/` on the release branch (live GitSpace) |
| 3 | Heuristic `previous_release_label()` — e.g. `R-2026-06-W4` → `R-2026-06-W3` |

### Check types added to the report

| Check name | Section | Type | Example |
|------------|---------|------|---------|
| Duplicate changeset IDs | DB | Deterministic | Duplicate `pganesan:1` in merged changelog |
| Changeset labels | DB | Deterministic | Missing `labels:Approved` |
| SQL syntax | DB | Deterministic | Unbalanced quotes in Approved changeset |
| AI insights | DB | Ollama (advisory) | Migration risk summary — never blocks |
| YAML value changes | YAML | Deterministic | `server.port`: 8080 → 9090 |
| AI new vs old | YAML | Ollama | Semantic config diff vs baseline |
| AI content review | YAML / Phrases | Ollama | Syntax, structure |

---

## 4. Data model

```mermaid
erDiagram
    TASK ||--o{ JOB : contains
    JOB ||--o{ SUB_TASK : contains
    TASK {
        string task_id
        string environment
        string jira_id
        string validation_status
        json validation_report
        json approval_chain
    }
    JOB {
        string job_id
        string section
        string release_branch
        string branch_from
        string branch_to
        json links
    }
    SUB_TASK {
        string sub_task_id
        string section
        string service_key
        string agent
        json steps
    }
```

---

## 5. Config map (where to change what)

```mermaid
flowchart LR
    subgraph Build["Build / MR"]
        GR["gitlab_repos.json<br/>service_key → repo path"]
    end

    subgraph Release["YAML / DB / Phrases on release repo"]
        CS["catalog_services.json<br/>dropdown names → folder name"]
        GJ["gitspace.json<br/>path templates"]
    end

    subgraph AI_CFG["AI"]
        ENV[".env<br/>VALIDATION_USE_AI=true<br/>OLLAMA_MODEL=..."]
    end

    GR --> BuildCheck["validation _validate_build_item"]
    CS --> ReleaseCheck["validation _validate_file_item"]
    GJ --> ReleaseCheck
    ENV --> AIClient["ai_client.py"]
```

---

## 6. Agentic roadmap (future)

```mermaid
flowchart TB
    subgraph Today["Implemented now"]
        T1["Deterministic validation"]
        T2["AI artifact review"]
        T3["AI new vs old YAML/DB diff"]
        T4["AI validation briefing"]
    end

    subgraph Next["Suggested next steps"]
        N1["Jenkins log AI verification<br/>mock_executor step 6"]
        N2["AI fix suggestions on failure"]
        N3["NL → deployment request composer"]
        N4["TaskDetail chat over report"]
    end

    subgraph Agentic["Agentic platform"]
        A1["Tool-calling agent layer"]
        A2["Validator / Merge / DB Guardian agents"]
        A3["Supervisor + human-in-the-loop"]
        A4["RAG over deployment history"]
    end

    Today --> Next --> Agentic
```

---

## 7. Environment variables (AI + validation)

| Variable | Purpose |
|----------|---------|
| `VALIDATION_USE_AI` | `true` to enable Ollama checks |
| `OLLAMA_MODEL` | e.g. `qwen2.5-coder:7b` |
| `OLLAMA_BASE_URL` | Default `http://localhost:11434` |
| `VALIDATION_BASELINE_RELEASE` | Optional fixed baseline for all diffs |
| `GITSPACE_MODE` | `live` for real GitSpace API |
| `GITSPACE_TOKEN` | Read API token for live mode |

---

## 8. Key files

| File | Role |
|------|------|
| `backend/validation.py` | Orchestrates all checks; new-vs-old for YAML/DB |
| `backend/ai_client.py` | `compare_artifacts()`, `analyze_artifact()`, `summarize_report()` |
| `backend/gitspace.py` | Baseline resolution, GitSpace client, URL builders |
| `backend/main.py` | API routes, `VALIDATION_USE_AI` gate |
| `frontend/src/components/ValidationReport.jsx` | Shows checks, baseline link, AI status |
