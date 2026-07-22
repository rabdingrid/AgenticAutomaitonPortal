# 05 — Phrases / JSON Validation (code walkthrough)

Deterministic JSON checks for Phrases / SchemaForms / NewSchemaForms. Paths under `deployment-portal/backend/`.

---

## 1. Files involved

| File | Role |
|------|------|
| `main.py` | Starts `run_validation` |
| `validators/validation.py` | `_validate_phrases_item`, `_json_checks`, `_phrases_service` |
| `gitspace.py` | `section_dir_for`, `resolve_section_files`, `get_file` |
| `orchestrator/jenkins_params.py` | `json_action_for_phrases_filename`, `sort_phrases_json_deployments` |
| `catalog.py` | Portal service lookup |

**No Ollama** in this section.

---

## 2. Routing into phrases

```python
# validation._validate_file_item
if section == "phrases":
    return _validate_phrases_item(link, release, cfg, client)
```

---

## 3. `_phrases_service` — force portal context

```python
def _phrases_service(link, service):
    out = dict(service)
    out["type"] = "portal"
    out["phrases_kind"] = link.get("sub_type") or out.get("phrases_kind") or "phrases"
    return out
```

**Explain:** Phrases always live under **portal** repo paths. `sub_type` selects folder kind:

| sub_type | Meaning later in Jenkins |
|----------|---------------------------|
| `phrases` | Update=`Json`, Action Add/Delete |
| `schemaforms` | Update=`SchemaForms` |
| `newschemaforms` | Update=`NewSchemaForms` |

---

## 4. `_validate_phrases_item` — load + parse

```python
# validation.py — _validate_phrases_item (concept)
service = _phrases_service(link, catalog.get_service(...))
# resolve folder for phrases_kind, list *.json
for each file:
    content = client.get_file(...)
    checks.extend(_json_checks(content))

if phrases_kind == "phrases" and json_files_meta:
    result["json_files"] = jenkins_params.sort_phrases_json_deployments(json_files_meta)
```

**Explain:** Pass message is a file count. Failures are per-file syntax. Only the **phrases** kind attaches `json_files` for orchestrator Delete/Add ordering.

---

## 5. `_json_checks` — the only rule

```python
def _json_checks(content: str) -> list[dict[str, str]]:
    try:
        json.loads(content)
        return [{"name": "JSON format validation", "status": "pass",
                 "detail": "Validation Passed"}]
    except json.JSONDecodeError as exc:
        return [{
            "name": "JSON format validation",
            "status": "fail",
            "detail": f"Line {exc.lineno} (column {exc.colno}): Invalid JSON syntax — {exc.msg}.",
        }]
```

**Explain:** No schema validation, no key rules — **syntax only**.

---

## 6. Action + sort (for Jenkins later)

```python
# jenkins_params.py
def json_action_for_phrases_filename(filename: str) -> str:
    # "remove" in name → Delete, else Add

def sort_phrases_json_deployments(entries):
    # Delete entries first, then Add
```

**Explain:** Explicit Delete-before-Add (safer than relying on filename sort alone).

---

## 7. Mental model

```
_validate_phrases_item
  → list JSON under portal Phrases/SchemaForms folder
  → json.loads each file
  → if phrases: sort Delete → Add into json_files
```

Downstream: [06-phrases-orchestrator.md](./06-phrases-orchestrator.md)
