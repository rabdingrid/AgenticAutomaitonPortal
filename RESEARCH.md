# Industry Research: How Other Companies Solve This

**What this document is:** A grounded comparison of our deployment portal
plan against how real companies and open-source platforms have solved the
same problem — self-service deployment requests, RBAC-gated repo access,
multi-step job orchestration, and audit history. Every claim below is
sourced; links are in Section 7.

**What this document is NOT:** A recommendation to adopt any of these
tools wholesale. The point is to borrow validated patterns and avoid
known failure modes — most of these companies spent years and real
production incidents discovering what we're about to design on paper.

---

## 1. The category we're actually building

What we're building has a name in the industry: an **Internal Developer
Platform (IDP)**, specifically the **self-service deployment** slice of
one. The standard anatomy, confirmed across every source reviewed:

```
Service Catalog  →  Self-Service Form  →  Orchestrated Actions  →  Audit Trail
(what exists)       (what you want)       (what happens)          (what happened)
```

This maps directly onto our plan:

| Our concept | Industry term |
|---|---|
| Service/repo catalog (S3 JSON) | Service Catalog |
| New Request form (Build/YAML/DB/Script) | Self-Service Action / Software Template |
| Task → Jobs → Orchestrator dispatch | Orchestrated Pipeline / Scaffolder Steps |
| History page | Audit Trail / Execution Log |
| RBAC + per-environment grants | Permission Framework |

We are not inventing a new category. We're building a **lightweight,
narrowly-scoped IDP** focused specifically on GitLab merge + Jenkins
build + Liquibase/YAML config — which is actually a *strength*: most
of the tools below are general-purpose and pay a real complexity tax
for capabilities we don't need yet.

---

## 2. Direct architectural comparisons

### 2.1 Backstage (Spotify) — Software Templates ≈ our New Request form

This is the closest published analog to what we designed. Backstage's
"Scaffolder" takes a YAML template with a `parameters` block (rendered
as a form) and a `steps` block (sequential actions, each with an
`action` name and `input`). A real Backstage GitLab template looks like this — note the repo picker field and the sequential merge-request-creating action:

```yaml
parameters:
  - title: Choose a location
    properties:
      repoUrl:
        ui:field: RepoUrlPicker     # <- exactly our "pick a project" dropdown
steps:
  - id: createGitlabGroup
    action: gitlab:group:ensureExists
  - id: publish
    action: publish:gitlab           # <- this is literally our merge action
```

**What validates our plan:** our `BuildJobSection` repo/MR dropdown is
structurally the same idea as Backstage's `RepoUrlPicker` field
extension— a form field type specifically built for picking a source-control location. Our "one job type per artifact kind" (build/yaml/db/script) is the same shape as Backstage's `steps` array, where each step loads code, templates variables, and publishes to a location like GitLab.

**What we should adopt:** Backstage explicitly warns against hand-rolling
authorization per-action. In their permission framework, policies are responsible for decisions and the plugins/backends are responsible for enforcing them — i.e., never let the UI decide who can do what; always re-check on the backend at the moment of action. Our `require_env_access()` call inside `main.py` route handlers (not just hiding dropdown options in the frontend) already follows this — confirmed correct.

**What to borrow for later phases, not now:** Backstage's scaffolder
supports a huge catalog of 146+ pre-built actions across providers (AWS,
Azure DevOps, Kubernetes, Sentry...), including things like waiting for a Kubernetes job to complete or merging HCL configuration files. We don't need this breadth — our 5 job types cover our actual workload — but if we ever need a "wait for external condition" pattern (e.g. wait for a Jira ticket to move to Approved before continuing), that's a precedent that this is a known, supported pattern elsewhere, not something exotic we're inventing.

---

### 2.2 Spinnaker (Netflix) — Pipeline orchestration + manual approval gates

Spinnaker's core abstraction is **Pipeline → Stage → Task**: a Stage is a collection of sequential Tasks describing a higher-level action, and stages can be sequenced in any order. This is our Task → Job → Steps hierarchy, validated independently at a company running between 2,000 and 5,000 pipelines per day, peaking at over 400,000 individual task executions — i.e., this three-level hierarchy holds up at serious scale, not just toy demos.

**Manual approval — directly validates our "merge happens as a distinct
action" design.** Spinnaker calls this a "Manual Judgment" stage: it requires manual approval prior to releasing an update, and crucially, Netflix's own engineers describe *why* they keep it even in a culture built around continuous deployment:

> "Engineers decide their own deployment strategy... In the continuous delivery pipeline, we give them the ability to add a manual judgment. I think it gives them the opportunity to build confidence in our tooling. It's like that last window before you check out of a retail store where you can still see everything one more time."

This is a strong validation for our `/jobs/{job_id}/merge` design — a
distinct, explicit action rather than something that happens silently
the instant a task is submitted.

**Netflix's stated philosophy is worth adopting verbatim for our admin
page design:**

> "We want to provide guardrails, not gates. I don't want to stop you from doing something — I want to give you context about why we think it might be a bad idea. I want to make sure that you have all the context to make that decision. But I don't want to prevent you from doing it."

This is a genuine design fork we should discuss: our current RBAC plan
is gate-based (hard 403 if you lack environment permission) rather than
guardrail-based (warn, but allow with extra approval). Gates are
correct for environment access (UAT/PROD should be hard-gated) but
Spinnaker's guardrail philosophy is worth considering for *risk
warnings* — e.g. "this Liquibase script contains a DROP TABLE, are you
sure?" as a soft warning rather than a hard block, matching what we
already discussed for the email-intake validator.

**Spinnaker's microservice breakdown maps almost 1:1 onto our backend
modules** — worth naming explicitly so the team sees we're not
inventing an unusual shape:

| Spinnaker component | Role | Our equivalent |
|---|---|---|
| Orca | orchestrates pipelines and tasks | `db.py`'s `_advance_orchestrator()` |
| Gate | API gateway | `main.py` |
| Fiat | authorization service — queries a user's access permissions for accounts and applications | `rbac.py` |
| Echo | eventing bus — sends notifications via Slack, email, SMS; acts on incoming webhooks | (not yet built — see Section 5) |
| Igor | triggers pipelines via CI jobs like Jenkins | `gitlab_client.py` (our Jenkins equivalent) |

**Known limitation we should plan around, not discover the hard way:**
Spinnaker's pipeline workflow is unidirectional — there is no option to backtrack if an error occurs or a step needs modification. Manual judgment steps can cause bottlenecks if multiple pipelines are queued at the same phase. Our sequential job dispatch (job 1 done → job 2 auto-starts) has the same one-directional limitation. Worth deciding now: if a Build job's merge fails, should the *whole task* go to `blocked` (current design) or should there be a retry-this-job-only path? Spinnaker chose to live with this limitation rather than build full backtracking — reasonable precedent for us to do the same in v1.

---

### 2.3 Northflank / Port / Humanitec — the "portal vs. platform vs. orchestrator" split

A recent comparison piece draws a distinction directly relevant to a
decision the team will eventually face:

> Port and Cortex are no-code commercial portals with customizable catalogs and self-service actions — portal only, no execution layer. Humanitec is a platform orchestrator that wraps existing Terraform and CI/CD tooling, but requires a separate portal. Northflank gives you the portal and the platform together.

**This validates our plan's scope.** We're building a portal *with* an
execution layer (we actually call GitLab/Jenkins, not just display
status) — putting us closer to Northflank's category than Port's. The
practical implication: if we ever outgrow our own backend, the
migration path is toward a Humanitec-style orchestrator-on-top-of-
existing-tooling, not toward a portal-only tool like Port (which would
require us to *keep* our execution layer and just skin a UI on top —
not a real simplification).

---

## 3. The security pattern we already got right (and a near-miss to avoid)

A 2026 security analysis of Backstage deployments in the wild found a
failure mode worth flagging explicitly, because it's the exact bug
class our RBAC test suite already checks for and prevents:

> Even with RBAC enabled, the default permission policies are permissive. The default guest policy in many configurations grants full read access to all catalog entities. Teams that enable RBAC without reviewing and tightening the default policies may believe they have access control when in practice every authenticated user can read every catalog entity, trigger every scaffolder template, and invoke every plugin action.

This is **fail-open** — exactly the bug pattern our `rbac.py` was
designed against. Our test suite (run during planning) confirmed: a
user with no matching Entra group grant gets **zero environments**, not
"developer with everything." This is the correct posture and the
research confirms it's a known, named failure mode others have actually
shipped to production — not a hypothetical risk.

**One more real near-miss worth flagging to the team**, since it touches
our form-input design directly:

> Backstage's scaffolder processes templates using Nunjucks. If template inputs are not properly sanitised, an attacker who controls input values can inject template syntax to execute arbitrary JavaScript in the scaffolder's execution environment, including all configured credentials and service accounts.

We don't use a templating engine like Nunjucks for our job execution
(our `db.py` builds `jenkins_params` as plain dict assembly, not string
interpolation into an executable template), so we are not directly
exposed to this exact vulnerability class. But it's a good reminder for
the `gitlab_client.py` and any future Jenkins-trigger code: never build
shell commands or template strings by concatenating user-supplied form
fields. Keep using structured parameter dicts (as planned) — exactly
what `_build_jenkins_params()` already does.

**Backstage's own permission framework documentation states the
principle we should hold ourselves to:**

> The plugin backend sends a request to the permission framework's backend with the authorization details... An authorization decision is sent to the plugin from the permission backend.

In other words: authorization is checked at the point of the backend
action, every time, not cached client-side or assumed from a prior page
load. Our plan's `require_env_access(user, environment)` called inside
each route handler (not just once at login) matches this.

---

## 4. RBAC implementation patterns worth comparing against ours

Backstage's community RBAC plugin uses a YAML/JSON conditional-policy
format that's more expressive than our v1 plan — worth knowing it
exists, not necessarily worth adopting yet:

```yaml
result: CONDITIONAL
roleEntityRef: role:default/test
resourceType: catalog-entity
permissionMapping: [read, update]
conditions:
  rule: IS_ENTITY_OWNER
  params:
    claims: [group:default/team-a, group:default/team-b]
```

This supports a "super user" concept with direct group membership only — explicitly noting that users who belong to a sub-group of a configured super user group will NOT inherit super-user access, to avoid accidental privilege escalation through nested group membership.

**This is a real gotcha worth pre-empting in our Entra group design**:
if your org's Entra groups are nested (a "DevOps-Leads" group nested
inside a broader "DevOps" group), our flat `entra_groups` list check in
`rbac.py` needs confirming — does Entra's `groups` claim already flatten
nested membership, or do we need to handle that ourselves? This is worth
a specific verification step before go-live, not an assumption.

**On policy storage**: Backstage's RBAC plugin supports storing policies in a database, using the same database configuration as the rest of Backstage, as an alternative to file-based policy config. Our plan keeps RBAC grants in the same S3 bucket as the rest of the catalog config — consistent with the "single source of config truth" goal, and a reasonable MVP choice, though if grant-editing frequency grows much higher than service-catalog editing frequency, a small DB table specifically for grants (with a proper audit log of *who changed a grant and when*) would be the natural next step — RBAC changes are exactly the kind of action you want a permanent audit trail for, more so than service catalog tweaks.

---

## 5. Gaps in our current plan, surfaced by this research

Going through these comparisons surfaced three things our v2 plan
doesn't yet address — not blockers, but worth a conscious decision
rather than silent omission:

### 5.1 No notification/event bus layer
Spinnaker's `Echo` component exists specifically because it supports sending notifications via Slack, email, SMS and acts on incoming webhooks — i.e., outbound notifications are treated as their own architectural layer, not bolted onto the orchestrator. Our plan has no equivalent yet. For now this is fine (frontend polling + manual checking is OK for an MVP demo), but if "tell the developer when their build job fails" becomes a real requirement, it deserves its own small module rather than being scattered across `db.py`'s `update_job_status()`.

### 5.2 No explicit "who approved this grant change" audit trail for RBAC itself
Section 4 above — flagged once already, repeated here because it's
the single highest-value addition relative to effort: a regulated
environment (which ours is, given the UAT/PROD environment tiers) will
eventually need to answer "who gave this developer UAT access, and
when" — not just "what does the current grant say."

### 5.3 No retry-single-job path, only retry-whole-task
Confirmed as a real limitation Spinnaker also accepted (Section 2.2)
rather than something we're uniquely missing — but worth a deliberate
"yes, accept this for v1" decision in the next planning conversation
rather than an unexamined gap.

---

## 6. Bottom line — should we be confident in our current plan?

**Yes, with the specific confidence coming from where the parallels
are, not just that parallels exist:**

- Our Task→Job hierarchy matches Spinnaker's Pipeline→Stage→Task model, validated at 400,000+ task executions/day at Netflix.
- Our form-driven repo/MR picker matches Backstage's `RepoUrlPicker` + scaffolder `steps` pattern, the most widely-adopted open-source IDP pattern in the industry today.
- Our RBAC fail-closed behavior (verified in testing) avoids the exact fail-open misconfiguration documented as a real, named vulnerability class in production Backstage deployments.
- Our "merge as a distinct, explicit action" design matches Netflix's own stated rationale for keeping manual judgment steps even in an org built around automation.

**The one place to bring a genuine discussion point back to the team**,
rather than just validation: Netflix's "guardrails, not gates"
philosophy (Section 2.2) is a real alternative philosophy to our
current hard-RBAC-gate design. Worth 10 minutes of discussion on
whether any of our environment gates should soften into warnings rather
than blocks — almost certainly not for UAT/PROD, but possibly for the
lower environments where speed matters more than ceremony.

---

## 7. Sources

| # | Source | URL |
|---|---|---|
| 1 | Octopus Deploy — What Is CI/CD? Complete 2026 Guide | https://octopus.com/devops/ci-cd/ |
| 2 | Harness — How to Build a Developer Self-Service Platform | https://www.harness.io/blog/how-to-build-a-developer-self-service-platform-that-actually-works |
| 3 | Northflank — Top internal developer portals in 2026 | https://northflank.com/blog/top-internal-developer-portals |
| 4 | Spacelift — What is an Internal Developer Platform (IDP)? | https://spacelift.io/blog/what-is-an-internal-developer-platform |
| 5 | Backstage — GitLab Discovery docs | https://backstage.io/docs/integrations/gitlab/discovery/ |
| 6 | Backstage — GitLab Locations docs | https://backstage.io/docs/integrations/gitlab/locations/ |
| 7 | hoop.dev — The Simplest Way to Make Backstage GitLab Work | https://hoop.dev/blog/the-simplest-way-to-make-backstage-gitlab-work-like-it-should |
| 8 | Roadie — Backstage GitLab Plugin | https://roadie.io/backstage/plugins/gitlab/ |
| 9 | Backstage — Software Templates docs | https://backstage.io/docs/features/software-templates/ |
| 10 | Backstage — Builtin scaffolder actions | https://backstage.io/docs/features/software-templates/builtin-actions/ |
| 11 | Red Hat Developer — Build your first Software Template for Backstage | https://developers.redhat.com/articles/2025/08/12/build-your-first-software-template-backstage |
| 12 | Roadie — Backstage Scaffolder Actions Directory | https://roadie.io/backstage/scaffolder-actions/ |
| 13 | Spinnaker.io — official site | https://spinnaker.io/ |
| 14 | Spinnaker.io — Concepts docs | https://spinnaker.io/docs/concepts/ |
| 15 | Octopus Deploy — Spinnaker: Key Features, Use Cases, Limitations | https://octopus.com/devops/spinnaker/ |
| 16 | The New Stack — How Netflix Built Spinnaker | https://thenewstack.io/netflix-built-spinnaker-high-velocity-continuous-delivery-platform/ |
| 17 | Salesforce Engineering Blog — Journey to Spinnaker Deployment Orchestration | https://engineering.salesforce.com/journey-to-spinnaker-deployment-orchestration-743bbda6c01c/ |
| 18 | OpsMx Blog — Spinnaker Basics in 5 mins | https://www.opsmx.com/blog/spinnaker-basics-in-5-minutes/ |
| 19 | Netflix TechBlog — Spinnaker Orchestration (Rob Fletcher) | https://medium.com/netflix-techblog/spinnaker-orchestration-19e7f7b88d33 |
| 20 | DevOpsSchool — What is Spinnaker & Spinnaker Architecture explained | https://www.devopsschool.com/blog/what-is-spinnaker-spinnaker-architecture-explained/ |
| 21 | AquilaX — Backstage Developer Portal Security: RBAC Bypass, Catalog Poisoning | https://aquilax.ai/blog/backstage-developer-portal-security |
| 22 | Backstage — Permission framework Overview | https://backstage.io/docs/permissions/overview/ |
| 23 | Backstage — Permission framework Concepts | https://backstage.io/docs/permissions/concepts/ |
| 24 | Roadie — Backstage OPA Permissions Wrapper Plugin | https://roadie.io/backstage/plugins/opa-permissions-wrapper/ |
| 25 | GitHub — backstage/community-plugins RBAC backend README | https://github.com/backstage/community-plugins/blob/main/workspaces/rbac/plugins/rbac-backend/README.md |
| 26 | npm — @backstage-community/plugin-rbac-backend | https://www.npmjs.com/package/@backstage-community/plugin-rbac-backend |
| 27 | Spotify for Backstage — RBAC plugin docs | https://backstage.spotify.com/docs/plugins/rbac |
| 28 | Cycloid — Internal Developer Platforms (IDPs) 2026's Top 11 | https://www.cycloid.io/cycloid_page/internal-developer-platforms-idps-2026s-top-11/ |
| 29 | CloudBees — CI/CD Workflows for Enterprise Software Delivery | https://www.cloudbees.com/capabilities/ci-cd-workflows |
| 30 | JetBrains Blog — Best CI/CD Tools for 2026 | https://blog.jetbrains.com/teamcity/2026/03/best-ci-tools/ |

---

## 8. Suggested next step

Bring this to the team with one specific decision to make, not just
"FYI here's some research": **do we want any environment gates to be
soft warnings (Spinnaker's "guardrails, not gates" model) instead of
hard 403s, for any environment tier below UAT?** Every other pattern in
our plan already matches industry precedent closely enough that this
is the one open design fork worth spending discussion time on, rather
than re-litigating the whole architecture.