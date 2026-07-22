# 07 — Build Validation (code walkthrough)

Merge preview / MR creation for microservices and portals. **No Ollama** here. Paths under `deployment-portal/backend/`.

---

## 1. Files involved

| File | Role |
|------|------|
| `main.py` | `run_validation` with `create_mrs=True` on submit, `False` on preview |
| `validators/validation.py` | `_validate_build_item` |
| `gitspace.py` | `preview_merge`, `create_merge_request`, `compare_url`, `merge_request_url` |
| `catalog.py` | Service → project path |

---

## 2. How build section is selected in `run_validation`

```python
# validation.run_validation
if section == "build":
    build_only = bool(sec.get("build_only"))
    for link in sec.get("links", []):
        bucket["items"].append(_validate_build_item(
            link,
            (sec.get("branch_from") or "").strip(),
            (sec.get("branch_to") or "").strip(),
            cfg, client, build_only,
            create_mrs=create_mrs,   # True on task submit bg, False on preview
        ))
```

**Explain:** Build uses **service repo** (feature → integ), not the release-repo YML/DB folders. `create_mrs` distinguishes preview vs opening a real MR.

---

## 3. `_validate_build_item` — code paths

### Build-only (no merge)

```python
if build_only:
    return {
        "status": "pass",
        "checks": [{"name": "Build only", "status": "pass",
                    "detail": "Build-only run — no merge, blank MergeID; Jenkins build will run directly."}],
        "merge": None,
        "ai_insight": None,
    }
```

**Explain:** Orchestrator will send blank `MergeID` to Titan jobs.

### Preview vs create MR

```python
if create_mrs:
    mr = client.create_merge_request(project, from_ref, to_ref, title)
else:
    mr = client.preview_merge(project, from_ref, to_ref)

mergeable = mr.get("mergeable")
has_conflicts = bool(mr.get("has_conflicts"))
```

**Explain:** Both return an MR-shaped dict. Preview should not require write access; create opens `iid` when allowed.

### Conflict fail

```python
if has_conflicts or mergeable is False:
    conflict_files = mr.get("conflicts") or mr.get("files_changed") or []
    conflict_text = ", ".join(conflict_files[:5]) or mr.get("detail", "merge conflict")
    checks.append({
        "name": "Merge conflicts",
        "status": "fail",
        "detail": f"Cannot merge {from_ref} → {to_ref}: {conflict_text}. Resolve in GitSpace…",
    })
```

**Explain:** Message lists **file paths from GitSpace**, not an AI “why/how to fix” report (not implemented).

### Success

```python
elif mergeable is True:
    # pass: MR opened (if create_mrs) OR "No conflicts — ready…"
```

Also attaches:

```python
"merge": {
    "iid", "mergeable", "has_conflicts", "conflicts",
    "files_changed", "commits_count", "changes_summary",
    "web_url", "source_branch", "target_branch", ...
},
"urls": { "merge_request", "compare", "gitspace" },
"ai_insight": None,
```

**Explain:** `changes_summary` is whatever GitSpace/compare API returns — **not** Ollama.

---

## 4. GitSpace methods to read

| Method | Purpose |
|--------|---------|
| `preview_merge(project, source, target)` | Read-only mergeability |
| `create_merge_request(...)` | Open MR on submit |
| `compare_url` / `merge_request_url` | Links for UI |

---

## 5. Mental model

```
build section
  → build_only? pass
  → preview_merge OR create_merge_request
  → conflicts? fail with file list
  → else pass + merge metadata for Jenkins MergeID
```

Downstream: [08-build-orchestrator.md](./08-build-orchestrator.md)
