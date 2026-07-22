# 01 — YAML Validation (code walkthrough)

Read this file to understand **YAML validation** without opening every source file. Paths are under `deployment-portal/backend/`.

---

## 1. Files involved

| File | Role |
|------|------|
| `main.py` | Starts validation (background / preview) |
| `validators/validation.py` | `run_validation`, `_validate_file_item`, `_yaml_checks` |
| `validators/yaml_validator.py` | All YAML rules (`validate_yaml` + helpers) |
| `gitspace.py` | Config, paths, `branch_exists`, `resolve_section_files`, `get_file` |
| `catalog.py` | `get_service(service_key)` → label/type |
| `orchestrator/jenkins_params.py` | `yaml_action_for_filename` (Remove vs Update) |
| `config/gitspace.json` | Base URL, templates, repos |
| `config/gitspace_release_folder_map.json` | Release folder name mapping |

**No Ollama** on the YAML item path (`ai_ran` stays `False`).

---

## 2. How validation is started

### Background (after create / revalidate)

```python
# main.py — _kick_off_validation / _run_validation_bg
def _run_validation_bg(task_id, environment, jira_id, sections_payload):
    db.set_validation_status(task_id, "running")
    report = validation.run_validation(
        environment, jira_id, sections_payload,
        use_ai=_validation_use_ai(),
        create_mrs=True,          # may create MRs for build section; YAML only reads files
    )
    db.set_validation_report(task_id, report)
```

**Explain:** Runs in a **daemon thread**. Status → `running`, then full report is saved on the task. UI polls the task for `validation_report`.

### Sync preview (form deep validate)

```python
# main.py — POST /validate/preview → validate_preview
report = validation.run_validation(..., create_mrs=False)
```

**Explain:** Same engine, no task persistence required; response is the report JSON.

---

## 3. Top-level router: `run_validation`

```python
# validators/validation.py — run_validation (~L598)
cfg = gitspace.load_config()
client = gitspace.get_client()

for sec in sections:
    section = sec.get("section")
    if section == "build":
        # ... _validate_build_item ...
    else:
        release = (sec.get("release_branch") or "").strip()
        for link in sec.get("links", []):
            bucket["items"].append(
                _validate_file_item(section, link, release, cfg, client, use_ai, environment)
            )
```

**Explain:**

1. Loads GitSpace config + API/mock client once.
2. For each form section, walks `links[]` (selected services).
3. Non-build sections (including **yaml**) go through `_validate_file_item`.
4. After all items: computes `stats`, `overall_status`, optional `summarize_report` (whole request, not per-YAML).

---

## 4. YAML item: `_validate_file_item`

```python
# validators/validation.py — _validate_file_item (~L380)
if section == "db":
    return _validate_db_item(...)
if section == "phrases":
    return _validate_phrases_item(...)

# --- YAML path continues here ---
service = catalog.get_service(link.get("service_key", "")) or {...}
project = gitspace._repo_for_section(section, gitspace.service_project_path(service, cfg), cfg)

if not client.branch_exists(project, release):
    # fail check "Branch exists"
else:
    files = gitspace.resolve_section_files(section, service, release, cfg, client)
    for fp in files:
        fname = fp.rsplit("/", 1)[-1]
        if section == "yaml":
            yaml_files_meta.append({
                "path": fp,
                "filename": fname,
                "action": jenkins_params.yaml_action_for_filename(fname),
            })
        content = client.get_file(project, release, fp)
        # _structural_checks + _yaml_checks for yaml
```

**Explain step by step:**

| Step | Code | Meaning |
|------|------|---------|
| 1 | `catalog.get_service` | Resolve human label / type from `service_key` |
| 2 | `_repo_for_section("yaml", …)` | Use **release repo** (not service app repo) for YAML files |
| 3 | `branch_exists` | Fail early if release branch missing |
| 4 | `resolve_section_files` | List `*.yml` / `*.yaml` under `…/YML/` |
| 5 | `yaml_action_for_filename` | `parameters-remove.yml` → `Remove`, else `Update` (for Jenkins later) |
| 6 | `get_file` | Download file content from GitSpace |
| 7 | `_yaml_checks` | Call `yaml_validator.validate_yaml` |

**Return value** includes `checks[]`, `urls.folder`, and `yaml_files` (for orchestrator sync). `ai_insight` is always `None` here.

---

## 5. Bridge to rules: `_yaml_checks`

```python
# validators/validation.py
def _yaml_checks(content: str, environment: str = "") -> list[dict[str, str]]:
    passed, messages = yaml_validator.validate_yaml(content, environment=environment)
    if passed:
        return [{"name": "YAML validation", "status": "pass", "detail": messages[0]}]
    return [{"name": "YAML validation", "status": "fail", "detail": msg} for msg in messages]
```

**Explain:** Converts validator’s `(bool, list[str])` into the UI check rows (`name` / `status` / `detail`).

---

## 6. Core rules: `yaml_validator.validate_yaml`

```python
# validators/yaml_validator.py — validate_yaml (~L78)
def validate_yaml(content: str, environment: str = "") -> tuple[bool, list[str]]:
    lines = content.splitlines()
    issues: list[YamlIssue] = []

    issues.extend(_check_tabs(lines))
    issues.extend(_check_colon_spacing(lines))
    issues.extend(_check_env_promotion_blocks(lines, environment))
    issues.extend(_check_syntax_and_duplicate_keys(content))

    if not _has_blocking_syntax_issue(issues):
        issues.extend(_check_indentation(lines))
        issues.extend(_check_block_scalars(lines))
        issues.extend(_check_list_alignment(lines))

    if issues:
        return False, [i.format() for i in unique_sorted_issues]
    return True, ["Validation Passed"]
```

**Explain — order matters:**

1. **Tabs / colon spacing** — style that breaks deploy tools  
2. **Env promotion** — need `{ENV}-*` or `COMMON-*` root key (e.g. `INTEG-ADD`, `COMMON-REMOVEKEY`)  
3. **Syntax + duplicate keys** — PyYAML + custom `_DupCheckLoader`  
4. **Indentation / blocks / lists** — only if file is still parseable  

### Env block check (important)

```python
# _check_env_promotion_blocks
if key.startswith("COMMON-"):
    has_common = True
elif key.startswith(f"{env}-"):   # e.g. INTEG-
    has_env = True
if has_common or has_env:
    return []  # OK
```

**Explain:** If **either** COMMON or the target env prefix exists at root, validation passes this rule. Missing both → fail.

---

## 7. GitSpace helpers (what to open)

| Function | File | Does |
|----------|------|------|
| `load_config()` | `gitspace.py` | Read `config/gitspace.json` |
| `get_client()` | `gitspace.py` | Mock or live GitLab API client |
| `service_project_path` | `gitspace.py` | Service → GitLab project path |
| `_repo_for_section` | `gitspace.py` | YAML/DB/phrases → release repo |
| `section_dir_for` | `gitspace.py` | e.g. `…/YML` folder |
| `resolve_section_files` | `gitspace.py` | List files by extension in folder |
| `get_file` | client method | Raw file text |
| `tree_url` | `gitspace.py` | Folder link for UI |

---

## 8. Output → orchestrator

`yaml_files` on the item is later read by `db._sync_yaml_deployments` to build `yaml_deployments[]` (see [02-yaml-orchestrator.md](./02-yaml-orchestrator.md)).

```text
parameters-remove.yml → action Remove  → Jenkins ACTION=Remove
parameters.yml        → action Update  → Jenkins ACTION=Update
```

---

## 9. Mental model

```
UI Validate
  → main._run_validation_bg / validate_preview
    → validation.run_validation
      → _validate_file_item("yaml")
        → gitspace resolve + get_file
        → yaml_validator.validate_yaml
      → report.sections[].items[]
    → db.set_validation_report (background path)
```

Next: [02-yaml-orchestrator.md](./02-yaml-orchestrator.md)
