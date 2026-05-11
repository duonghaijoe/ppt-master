# Multi-Tenant Re-Architecture Plan

Status: proposal — not started.
Owner: chau.duong@katalon.com
Last updated: 2026-05-11

## 1. Goals

- One Awesome Deck instance hosts many tenants. A tenant is an organisation
  (or a personal workspace) that owns its own projects, templates, and
  design system.
- A user can belong to multiple tenants with different roles in each.
- Projects can be shared across tenants on a case-by-case basis.
- Tenants never see backend code, infra config, repo internals, or each
  other's data.
- Platform-level resources (`skills/`, `examples/`, `docs/`, scripts) are
  visible only to platform admins. The agent may read `skills/` as a tool
  reference, but tenants cannot inspect or modify it.

## 2. Non-goals (defer to later iterations)

- Real identity provider integration (OIDC / SAML). We will start with a
  header-based identity contract (`X-User-Id`) behind an external reverse
  proxy. Slotting in a real IdP later does not require redesigning the
  application authz layer.
- Billing, quotas, rate limiting per tenant.
- Audit log persistence (we will emit events; storing them is a follow-up).
- Soft delete / trash UX (hard delete only, like today).
- Bulk migration of historical exports/sources between tenants.

## 3. Three-Scope Model

Every resource in the system lives in exactly one of three scopes.

| Scope    | Owner                | Examples                                       | Tenant access     |
|----------|----------------------|------------------------------------------------|-------------------|
| Platform | Platform admin       | `skills/`, `examples/`, `docs/`, scripts, code | Read-only via API; never via filesystem; agent may read `skills/` only |
| Tenant   | Tenant owner / admin | Templates, design system, shared brand assets, members, roles | Members per role  |
| Project  | Tenant + project ACL | Sources, slides, exports, project SKILL.md     | Default = tenant role; per-project ACL overrides |

A tenant is the unit of billing and ownership. A project belongs to exactly
one tenant but can be *shared* with users from other tenants via an explicit
ACL grant.

## 4. Target Filesystem Layout

```
<repo-root>/
  platform/                              # admin-only; tenants never read or write directly
    skills/                              # moved from <repo-root>/skills/
    examples/
    docs/
    scripts/                             # if/when we centralize ops scripts

  tenants/
    <tenant-slug>/                       # slug: [a-z0-9][a-z0-9-]{0,62}
      config.json                        # { name, owner_user_id, default_format, created_at }
      members.json                       # [{ user_id, role: "owner"|"editor"|"viewer" }]
      design_system/                     # tenant-wide tokens
        palette.json                     # named colors
        typography.json                  # font stacks
        spec.md                          # narrative design rules
      templates/<template-name>/         # current top-level templates/, scoped to tenant
        template.json
        SKILL.md, design_spec.md, ...
        images/                          # brand assets curated by agent
        thumbnail.svg
      shared/                            # brand assets reusable across projects
        images/, logos/, fonts/
      projects/<project-name>/           # current top-level projects/
        sources/, svg_output/, svg_final/, exports/, ...
        SKILL.md
        .project.json                    # { acl: [{user_id, role}], shared_with: [tenant_slug] }

  users/
    <user-id>.json                       # { email, display_name, memberships: [{tenant_slug, role}], platform_admin: bool }

  .platform.json                         # { admins: [user_id], created_at, schema_version }
```

Notes:
- The current `projects/` and `templates/` directories at the repo root move
  inside `tenants/default/`. The migration script handles this.
- `skills/` moves to `platform/skills/`. CLAUDE.md and SKILL.md path
  references are updated. The agent's read sandbox is widened to allow
  `platform/skills/` specifically.
- User IDs are opaque strings (we pick UUIDs). The mapping email → id is
  kept in the user file so identity-provider migrations are simple.

## 5. Identity Model

Phase 1 contract: a reverse proxy in front of the FastAPI app injects two
headers on every request:

```
X-User-Id:    <opaque-id>
X-User-Email: <email>
```

The backend trusts these headers unconditionally. In local dev, a thin
shim sets them from a `DEV_USER_ID` env var. In production, the proxy is
the authentication boundary (Auth0, Ory, Cloudflare Access, etc.).

The FastAPI app:
1. Resolves `X-User-Id` to a user record (`users/<id>.json`), creating one
   on first sight using `X-User-Email`.
2. Computes the user's `memberships` and `platform_admin` flag.
3. Stores both on `request.state.user` for downstream handlers.

Phase 2 (later): swap the header contract for a real session cookie + IdP
integration. The internal `user_id` and role logic do not change.

## 6. Authorization

### 6.1 Tenant roles

| Role   | Read | Create project | Edit project | Manage templates | Manage design system | Manage members |
|--------|------|----------------|--------------|------------------|----------------------|----------------|
| owner  | yes  | yes            | yes          | yes              | yes                  | yes            |
| editor | yes  | yes            | yes          | yes              | no                   | no             |
| viewer | yes  | no             | no           | no               | no                   | no             |

### 6.2 Project ACL

A project carries `.project.json` with optional per-user grants:

```json
{
  "acl": [{"user_id": "u-123", "role": "editor"}],
  "shared_with_tenants": ["acme"]
}
```

`acl` grants apply to specific users regardless of which tenant they belong
to. `shared_with_tenants` is a coarser grant: every member of the named
tenant gets the same role as in their home tenant, capped at `editor`.

Resolution order when a request hits a project:
1. If the user is `platform_admin`, allow.
2. If the user has any role in the project's home tenant, use it.
3. If the project's `acl` lists the user, use that role.
4. If any of the user's tenants appears in `shared_with_tenants`, treat as
   `editor` (or `viewer` for that tenant's own viewers).
5. Otherwise: 403.

### 6.3 Platform role

A user with `platform_admin: true` can:
- Create / delete tenants.
- See every tenant.
- Edit `platform/` content (skills, docs, examples).
- Manage the platform admin list.

Platform admin is set in `.platform.json`. Bootstrap: the first user to
hit `/api/me` on a fresh deploy is recorded as the sole admin (configurable
via `BOOTSTRAP_ADMIN_EMAIL`).

## 7. API Surface

Every existing endpoint moves under a tenant prefix or is reclassified as
platform / user scope. We keep the route shape as close to current as
possible to limit churn.

### 7.1 User & session

```
GET    /api/me                                     # current user, memberships, platform_admin
POST   /api/sessions                               # body: { tenant, project_name, format, permission_mode, model_tier }
POST   /api/sessions/attach                        # body: { tenant, project_name, ... }
POST   /api/sessions/{sid}/permission_mode
POST   /api/sessions/{sid}/model_tier
POST   /api/sessions/{sid}/interrupt
POST   /api/sessions/{sid}/permission
DELETE /api/sessions/{sid}
POST   /api/sessions/{sid}/message
POST   /api/sessions/{sid}/import/file
POST   /api/sessions/{sid}/import/files
POST   /api/sessions/{sid}/import/url
```

A session is bound to `(user_id, tenant, project)` at creation. The
backend rejects messages from a different user even with the right
`session_id`.

### 7.2 Tenants

```
GET    /api/tenants                                # tenants the caller is a member of (+ admin sees all)
POST   /api/tenants                                # platform_admin only — body: { slug, name, owner_user_id }
GET    /api/tenants/{t}
PATCH  /api/tenants/{t}                            # owner only — name, default_format
DELETE /api/tenants/{t}                            # platform_admin only

GET    /api/tenants/{t}/members
POST   /api/tenants/{t}/members                    # owner only — body: { user_id|email, role }
PATCH  /api/tenants/{t}/members/{user_id}          # owner only — body: { role }
DELETE /api/tenants/{t}/members/{user_id}          # owner only

GET    /api/tenants/{t}/design-system              # palette, typography, spec
PUT    /api/tenants/{t}/design-system              # owner only

GET    /api/tenants/{t}/shared                     # list shared assets
POST   /api/tenants/{t}/shared                     # multipart upload
DELETE /api/tenants/{t}/shared/{path}
```

### 7.3 Templates (tenant-scoped)

```
GET    /api/tenants/{t}/templates
GET    /api/tenants/{t}/templates/{name}
GET    /api/tenants/{t}/templates/{name}/file?path=...
POST   /api/tenants/{t}/templates                  # body: { source_project, name, description, include_images[] }
DELETE /api/tenants/{t}/templates/{name}
POST   /api/tenants/{t}/projects/from-template     # body: { template, project_name, format? }
```

A template is owned by exactly one tenant. Cross-tenant template reuse
goes through a separate "publish to platform catalog" workflow that we
defer to a future phase.

### 7.4 Projects

```
GET    /api/tenants/{t}/projects
POST   /api/tenants/{t}/projects                   # body: { name, format }
DELETE /api/tenants/{t}/projects/{p}               # editor+; cascades exports
GET    /api/tenants/{t}/projects/{p}/...           # slides, exports, recent, tree, file, web, svg, output-dirs, events, export.pptx, images
POST   /api/tenants/{t}/projects/{p}/...           # svg-save, output-dirs/register, output-dirs/current
PUT    /api/tenants/{t}/projects/{p}/output-dirs

POST   /api/tenants/{t}/projects/{p}/share         # owner+ — body: { user_id|tenant_slug, role }
DELETE /api/tenants/{t}/projects/{p}/share/{principal}
```

### 7.5 Platform

```
GET    /api/platform/skills                        # list skills the agent can use (read-only)
GET    /api/platform/skills/{name}/file?path=...
GET    /api/platform/admins                        # platform_admin only
POST   /api/platform/admins                        # platform_admin only
```

## 8. Session and Agent Model

Each session record holds:

```python
@dataclass
class Session:
    id: str
    user_id: str
    tenant: str
    project_path: Path          # tenants/<t>/projects/<p>/
    role: str                   # the user's effective role on this project
    permission_mode: str
    model: Optional[str]
    ...
```

`registry.create()` takes `(user_id, tenant, project_path, role)`. Every
SSE delivery and permission resolution checks `session.user_id == request_user`
before yielding. The `role` is consulted before the sandbox check for
`viewer` denials (a viewer cannot Write/Edit anything).

## 9. Sandbox

Replace the current single-project sandbox with a tenant-aware version.

### 9.1 Allow-lists

| Tool family       | Allow                                                              |
|-------------------|--------------------------------------------------------------------|
| Read              | `tenants/<t>/projects/<p>/`, `tenants/<t>/templates/`, `tenants/<t>/design_system/`, `tenants/<t>/shared/`, `platform/skills/` |
| Write / Edit      | `tenants/<t>/projects/<p>/` only                                   |
| Bash writes       | Same as Write / Edit                                               |
| Network (Bash)    | Loopback only (the in-tree API) and a small allow-list of curl-able domains (image generation, etc.) — same as today |

### 9.2 Deny rules added

- Any path under `tenants/<other>/`: deny both read and write.
- Any path under `platform/` except `platform/skills/`: deny read.
- Any path under `users/` or `.platform.json`: deny read and write.
- Any path under `web/backend/`, `web/frontend/`, `.venv/`, repo-root
  config files: deny read and write (already true; widen).
- Bash writes targeting `tenants/<t>/templates/`, `tenants/<t>/shared/`,
  `tenants/<t>/design_system/` are denied. These resources are managed
  through the API, with the same redirect-style error message the agent
  already sees today for `output_dirs.json`.

### 9.3 Path resolution

Every project-relative path the agent uses is resolved against
`session.project_path` instead of a global `PROJECTS_DIR / name`. The
existing `safe_rel`, `project_path`, `list_slides`, `list_recent`,
`watch_project`, etc. take a tenant + project pair or a fully-resolved
project path.

## 10. Frontend Changes

### 10.1 Bootstrap

`/api/me` is fetched first thing on app load. The response shape:

```json
{
  "user": { "id": "u-1", "email": "...", "display_name": "..." },
  "platform_admin": false,
  "tenants": [
    { "slug": "default", "name": "Default", "role": "owner" },
    { "slug": "acme", "name": "Acme Co.", "role": "editor" }
  ]
}
```

The UI picks the most-recently-used tenant from local storage; falls back
to the first in the list. If `tenants` is empty, show an empty-state
"Ask your platform admin to add you to a tenant".

### 10.2 Tenant switcher

A persistent switcher in the top bar (replaces or sits next to the project
switcher). Switching tenants:
- Resets the dashboard to that tenant's projects + templates.
- Cancels any in-flight session.
- Updates the path prefix for every API call from `/api/tenants/<old>/...`
  to `/api/tenants/<new>/...`.

### 10.3 Tenant settings page

Visible to `owner` only:
- Members + role assignment.
- Design system editor (palette, typography, spec markdown).
- Shared brand asset uploader.
- Tenant info (name, default format).

### 10.4 Platform settings page

Visible to `platform_admin` only:
- Create tenant, list tenants, set tenant owner.
- Manage other platform admins.
- View skill catalog (read-only listing of `platform/skills/`).

### 10.5 Project sharing

In the existing project header dropdown, add a "Share..." item that opens
a modal: pick user-by-email or tenant slug, choose role, save. The list
of current grants is shown above the form.

### 10.6 Dashboard changes

- Templates tab stays but is now per-tenant: only the active tenant's
  templates show.
- Project cards show a shared-from-other-tenant badge when applicable.
- "New project" form remains but writes into the active tenant.

## 11. Backend Module Changes

### 11.1 `files.py`

- Replace `PROJECTS_DIR = REPO_ROOT / "projects"` with
  `def tenant_dir(t) -> Path` and `def project_path(t, name) -> Path`.
- Every function that takes a bare `name` gets an additional `tenant`
  parameter. The free functions (e.g., `list_recent`, `watch_project`,
  `read_output_dirs`, `register_output_dir`) follow suit.
- `safe_rel(tenant, name, path)` does the existing containment check
  against the tenant + project root.

### 11.2 `templates.py`

- `TEMPLATES_DIR` becomes a function: `templates_dir(tenant)`.
- All helpers take `tenant` as first argument.
- `save_project_as_template(tenant, source_project, name, ...)`.
- `create_project_from_template(tenant, template, new_project, fmt)`.
- Template metadata adds `tenant` field.

### 11.3 `agent.py`

- `Session` gains `user_id`, `tenant`, `role`.
- `_sandbox_deny` reads `session.project_path` (already does, indirectly)
  + a per-session deny-list of sibling tenants.
- `GUARDRAIL_SUFFIX` is rewritten so the agent talks about "this project"
  and "this workspace" without naming `tenants/`, `users/`, or any
  multi-tenant internals.
- The Templates section already calls the API; we update the curl example
  to include the active tenant.

### 11.4 `main.py`

- Add `Depends(current_user)` to every route except `/api/health`.
- Add `Depends(require_tenant_role(min_role))` helpers used per route.
- Path operations move under `/api/tenants/{tenant}/...` prefixes.
- Add tenant + member + design-system + shared-asset endpoints.
- Backwards compatibility: the existing `/api/projects/{name}/...` and
  `/api/templates/...` routes redirect to the `default` tenant for a
  deprecation window so the current frontend keeps working during migration.

### 11.5 New module: `tenants.py`

- CRUD over `tenants/<slug>/config.json` and `members.json`.
- Role checks.
- Member resolution by email (creates user records on demand for invites).

### 11.6 New module: `users.py`

- `get_or_create(user_id, email)` from request headers.
- Memberships, platform_admin lookup.

### 11.7 New module: `authz.py`

- `current_user(request)` dependency.
- `require_tenant_role(tenant, min_role)` helpers.
- `require_platform_admin`.
- `resolve_project_role(user, tenant, project)`.

## 12. Migration Plan

We migrate in-place with a script. Existing state ends up inside a single
`default` tenant. The script is idempotent.

### 12.1 Script: `scripts/migrate_to_multitenant.py`

Steps (each guarded by a presence check so reruns are safe):

1. `mkdir -p tenants/default/{projects,templates,design_system,shared/images}`.
2. `mkdir -p platform users`.
3. Move `projects/*` to `tenants/default/projects/`.
4. Move `templates/*` to `tenants/default/templates/`.
5. Move `skills/` to `platform/skills/`. Update CLAUDE.md and SKILL.md
   references via sed (careful: only the project-root CLAUDE.md, never
   project-local SKILL.md files).
6. Move `examples/`, `docs/` to `platform/` (or leave at repo root if you
   prefer; the agent's read sandbox just whitelists wherever they land).
7. Write `tenants/default/config.json` (name "Default", owner = bootstrap
   admin), `members.json` (the admin as owner).
8. Write `.platform.json` with the bootstrap admin user id.
9. Write `users/<bootstrap-id>.json`.
10. Write `tenants/default/design_system/{palette.json,typography.json,spec.md}`
    by extracting tokens from any `projects/*/SKILL.md` we can parse;
    otherwise emit empty scaffolds.

### 12.2 Path-mapper compatibility layer (intermediate state)

Before the full route refactor lands, we can route `/api/projects/{name}`
through a path-mapper that resolves `name` to
`tenants/default/projects/name`. This lets the migration land in two
shippable PRs:

- PR 1: introduces `tenants/default/` layout + path mapper. Frontend
  unchanged. No new endpoints. Verifies nothing regressed.
- PR 2: introduces real tenant routes + frontend tenant switcher + authz.

## 13. Phased Delivery

Each phase ships independently. Verification at each step listed.

### Phase 1 — Filesystem layer (no behaviour change)

- Add `tenants/default/` and path-mapper.
- Move templates + projects under it.
- Verify: existing UI works end-to-end.

### Phase 2 — Identity + `/api/me`

- Header-based identity, user records, dev shim.
- `/api/me`, `/api/tenants` (read-only).
- Verify: dashboard fetches `/api/me`; no role enforcement yet.

### Phase 3 — Role enforcement

- Add `current_user` and `require_tenant_role` dependencies on every
  route.
- Tenant-prefix the routes; keep legacy routes as redirects to `default`.
- Verify: viewer cannot write; editor cannot manage members; owner can.

### Phase 4 — Platform scope

- Move `skills/` → `platform/skills/`. Update agent prompt + read
  sandbox whitelist. Update CLAUDE.md path references.
- Add `/api/platform/skills` endpoints.
- Verify: tenant agent can still read skills; tenants cannot list
  `platform/admins`.

### Phase 5 — Tenant management UI

- Tenant switcher, members page, design-system editor, shared asset
  uploader.
- Templates tab becomes tenant-scoped in the UI.

### Phase 6 — Cross-tenant sharing

- Project `.project.json` ACL.
- `/api/tenants/{t}/projects/{p}/share` endpoints.
- Share dialog in the project header.

### Phase 7 — Hardening / nice-to-haves

- Audit log emission.
- Rate limiting per tenant.
- Quotas (max projects, max storage).
- Soft delete + trash.

## 14. Open Questions

1. Where do API keys for image generation live? Per tenant (so each can
   bring its own) or shared at the platform level? Today they're env-only
   on the host. Suggest: platform-level by default, optional override at
   the tenant level via `tenants/<t>/secrets.json` (encrypted at rest).
2. Should we surface a "platform catalog" of templates that any tenant
   can fork? Useful for onboarding but introduces a publish/approve flow.
   Defer to a later phase.
3. Project rename within a tenant: do we lock it (current behaviour) or
   support it? Templates and exports reference the project directory name
   by string. A rename today would break sources. Decide before Phase 3.
4. How do we expose the agent's reasoning that it read a skill file? The
   current redaction strips `skills/` from chat. Once skills live under
   `platform/skills/`, the same redaction needs to apply. We may want to
   surface "consulted N skill references" as a UI affordance instead of
   raw paths.
5. Domain mapping per tenant (custom URLs)? Out of scope for now; assume
   a single host with path-based routing.

## 15. Risks

- **Authz drift**: every new endpoint must declare its role requirement.
  We mitigate by making the `current_user` dependency mandatory in a
  router-level dependency, so forgetting it is a startup failure (route
  inspection at boot) instead of a silent leak.
- **Migration trips up project SKILL.md path references**. We do a
  pre-flight scan and only rewrite the project-root CLAUDE.md, never
  per-project SKILL.md files. Acceptance gate: every existing project
  still exports successfully after Phase 1.
- **Sandbox regression**: the current sandbox is well-tested for the
  single-project case. The new tenant-aware sandbox needs a test suite
  before Phase 3 ships — block writes / reads across tenants, allow
  reads to `platform/skills/`.
- **Sessions outliving role changes**: if an owner demotes a user mid-
  session, the SSE stream keeps yielding. We re-check `role` on every
  message send and at every tool-permission boundary.

## 16. Acceptance Criteria for "Multi-Tenant Done"

- Two tenants exist on the same host; their dashboards show disjoint
  project lists, template lists, and design systems.
- A viewer in tenant A cannot mutate any file in tenant A.
- A user with membership in tenants A and B can switch between them and
  agents in either tenant cannot read the other's files even when given
  the path.
- A project shared from tenant A to tenant B appears in tenant B's
  dashboard with the granted role; the share can be revoked.
- A platform admin can create / delete tenants and is the only role that
  can edit `platform/skills/`.
- The migration script has been run against a copy of the current state
  and every existing project still exports successfully.

## 17. Out of Scope (explicit)

- Real authentication (OIDC, SAML, magic links).
- Billing and metering.
- Per-tenant theming of the web app chrome.
- Multi-region deployment.
- Realtime collaboration (multiple users in the same chat).
- API tokens for programmatic access.
