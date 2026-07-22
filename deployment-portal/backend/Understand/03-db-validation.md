# 03 — DB / Liquibase Validation (code walkthrough)

How SQL on the release branch is checked. **This is the main validation AI path.** Paths under `deployment-portal/backend/`.

---

## 1. Files involved

| File | Role |
|------|------|
| `main.py` | Starts `run_validation` (same as YAML) |
| `validators/validation.py` | `_validate_db_item` — orchestration of DB checks + AI |
| `validators/liquibase_validator.py` | Parse changesets, structure, sqlglot, report format |
| `ai_client.py` | `review_db_sql_syntax`, polish findings, optional `review_db_changelog` |
| `gitspace.py` | `resolve_db_files`, `get_file`, `db_filenames_for_environment` |
| `catalog.py` | Service label |

Env flags: `VALIDATION_USE_AI`, `VALIDATION_DB_SQL_AI`, `VALIDATION_DB_SQL_BUDGET_S`, `VALIDATION_DB_SQL_NUM_PREDICT`.

---

## 2. Entry into DB item

```python
# validation.run_validation → _validate_file_item
if section == "db":
    return _validate_db_item(link, release, environment, cfg, client, use_ai)
```

---

## 3. `_validate_db_item` — full pipeline

### 3a. Locate and load SQL

```python
# validators/validation.py — _validate_db_item (~L138)
project = gitspace._repo_for_section("db", gitspace.service_project_path(service, cfg), cfg)
file_paths = gitspace.resolve_db_files(environment, service, release, cfg, client)
# must include Common.sql
file_parts = []
for fp in file_paths:
    content = client.get_file(project, release, fp)
    file_parts.append((fname, content))
```

**Explain:** `resolve_db_files` probes env-specific names (e.g. `Common.sql` + `INTEG.sql`). Missing Common → hard fail.

### 3b. Structural Liquibase check (defer SQL)

```python
merged = liquibase_validator.merge_db_contents(file_parts)
lb_checks, meta = liquibase_validator.validate_merged_changelog(
    merged,
    validated_files=validated_names,
    environment=environment,
    file_parts=file_parts,
    defer_sql_syntax=use_ai and ai_client.db_sql_review_enabled(),
)
```

**Explain:**

- `merge_db_contents` concatenates files with markers for one logical changelog.  
- `validate_merged_changelog` checks headers, labels (Approved/Pending), empty bodies, duplicates, dangerous SQL.  
- `defer_sql_syntax=True` means **do not** run sqlglot inside this function; leave executable SQL for the AI/sqlglot stage below.

### 3c. Flag broken changesets → Ollama → apply

```python
review_ctx = liquibase_validator.build_ai_sql_review_contexts(file_parts, meta)
flagged_ctx = [
    c for c in review_ctx
    if liquibase_validator.changeset_has_syntax_issues(file_parts, c["changeset"])
]

if not flagged_ctx:
    # all clean — no Ollama calls
    apply_ai_sql_review_findings(..., ai_findings={}, ...)
else:
    review_result = ai_client.review_db_sql_syntax(label, flagged_ctx, environment=environment)
    apply_ai_sql_review_findings(..., ai_findings=review_result.findings, ...)
    # any flagged but missing from AI → deterministic sqlglot messages
    append_deterministic_sql_syntax_for_changesets(..., fallback_cs)
```

**Explain:**

| Piece | Role |
|-------|------|
| `build_ai_sql_review_contexts` | Numbered SQL snippet per Approved changeset |
| `changeset_has_syntax_issues` | Fast **sqlglot** true/false |
| `review_db_sql_syntax` | **One Ollama call per flagged CS**, stops after time budget |
| `apply_ai_sql_review_findings` | Turn AI JSON into `DbFinding` + hints |
| `append_deterministic_…` | Fallback text if Ollama skipped/failed |

Clean SQL ⇒ **zero** Ollama calls for that service.

---

## 4. Liquibase parser (core)

```python
# liquibase_validator.py — parse_changesets
_CHANGESET_HEADER = re.compile(
    r"^[^\S\n]*--\s+changeset\s+(\S+)(?:\s+(.*))?$",
    re.IGNORECASE | re.MULTILINE,
)
# line_number = content[:match.start()].count("\n") + 1
```

**Explain:** Leading whitespace is **spaces/tabs only** (`[^\S\n]*`), not newlines — fixes off-by-one when blank lines precede `-- Changeset`.

```python
def _changeset_display_id(cs) -> str:
    if cs.author and cs.changeset_id:
        return f"{cs.author}:{cs.changeset_id}"
    return "(invalid author:id)"  # e.g. header was "-- Changeset :"
```

**Explain:** UI never shows bare `:` as the changeset name.

Malformed IDs get a structural error and are **skipped** for AI review contexts.

---

## 5. Ollama review (ai_client)

```python
# ai_client.py — review_db_sql_syntax
budget = float(os.getenv("VALIDATION_DB_SQL_BUDGET_S", "10"))
for ctx in contexts[:12]:
    if time.monotonic() - start > budget:
        failed.add(cs)   # deterministic fallback later
        continue
    items = _review_one_changeset(...)  # one _generate() to Ollama
```

**Explain:** Per-changeset (not one big batch) so line numbers stay correct. Budget keeps validation under ~10s; leftovers still show sqlglot errors.

`_polish_sql_finding` normalizes messages like `Misspelled keyword SELEC — did you mean SELECT?` and strips noisy “on line N” from hints (header already shows location).

---

## 6. Report formatting

```python
# liquibase_validator — _format_db_report / _format_findings_grouped
# ▸ author:id
#    Common.sql · `-- Changeset` at line N
#    <message>
#    💡 <hint>
```

Built via `_compact_checks` → single check `detail` string on the validation item.

---

## 7. Mental model

```
_validate_db_item
  → load Common.sql (+ env.sql)
  → validate_merged_changelog (structure only if defer)
  → sqlglot flags bad Approved changesets
  → Ollama polishes those (budgeted)
  → fallback sqlglot text for the rest
  → UI report by changeset
```

Downstream Jenkins: [04-db-orchestrator.md](./04-db-orchestrator.md)
