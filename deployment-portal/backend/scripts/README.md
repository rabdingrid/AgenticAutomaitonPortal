# Backend scripts

Run from `deployment-portal/backend/`:

| Script | Purpose |
|--------|---------|
| `trigger_build.py` | Fire Titan-Microservices or Titan-Portals build (build-only, blank MergeID) |
| `run_validation.py` | Run the full validation pipeline without the portal |
| `check_gitspace.py` | Verify GitSpace token and branch access |
| `discover_catalog.py` | Reconcile service catalog against GitSpace repos |
| `list_unmapped_repos.py` | Find repos missing from `config/gitlab_repos.json` |
| `hash_password.py` | Generate bcrypt hash for `users.json` |
| `graph_oauth_login.py` | One-time Microsoft Graph OAuth for email notifications |
| `test_graph_mail.py` | Send a test email via Graph API |

Example:

```bash
cd deployment-portal/backend
set -a && source .env && set +a
.venv/bin/python scripts/trigger_build.py Shipping R-2026-07-W2
```
