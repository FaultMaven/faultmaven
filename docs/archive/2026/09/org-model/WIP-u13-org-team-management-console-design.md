# WIP — U13: Org/Team Management Console (design for sign-off)

**Campaign:** Org-Model Alignment (Phase 3), Wave 5 / U13. See `WIP-org-model-alignment-plan.md`.
**Basis:** ADR-013 (canonical Enterprise/Organization/Team) + ADR-010 (one codebase; hosted management/admin console = composed proprietary module, D7).
**Status:** DRAFT — design-first per owner decision (2026-07-20). Implementation follows sign-off, in sequenced PRs.

---

## 1. Goal & non-goals

**Goal:** the management surface that lets an org admin **create/rename teams, add/remove members, and invite users** — the piece that finally populates `teams` / `team_members` at runtime so the team-sharing foundation (Wave 4, U7–U10) has data to resolve. Plus the minimal **organization** admin (view org, manage org membership + roles) that team management sits inside.

**Non-goals (this unit):** billing/metering; SSO/SCIM provisioning; enterprise-tier admin; cross-org "platform" admin; any change to the sharing/allowlist mechanics (done in Wave 4). Invitations are in scope but see §6 for the minimal v1.

**Placement (ADR-010 D7):** the management API is the **composed proprietary module** → it lives in the private **`faultmaven-cloud`** repo, NOT open-core. Open-core ships only the substrate (already present: `ITeamRepository`, `IOrganizationRepository`, `TeamService`, repos, RBAC). The dashboard console UI lives in `faultmaven-dashboard` (open) but is **capability-gated off** unless the cloud backend advertises it.

---

## 2. Current ground truth (from recon, 2026-07-20)

- **Cloud repo is a near-empty skeleton.** Only real code is `providers/tenant_llm_router.py`. `api/`, `middleware/`, `config/` are empty stub packages. Management API is **greenfield**.
- **No cloud composition root.** There is no `create_app()` factory in core (core builds a module-level singleton `app = FastAPI(...)` and registers routers at import time). There is **no `faultmaven_cloud/main.py`**, yet the cloud `Dockerfile` CMD is `uvicorn faultmaven_cloud.main:app` — a **dangling entrypoint**. This must be built first (§4).
- **Multi-tenant is not bootable.** `MULTI_TENANT_READY = False` (`faultmaven/providers/tenancy/factory.py`) fails **closed** at both the tenancy factory and the startup coherence gate. `create_team_service()` returns `None` until multi-tenant is active. So the console is **inert until ADR-010 P2** (request→org tenant-context wiring; the RLS schema already landed). We build against the substrate and unit-test it; runtime activation waits on P2.
- **Substrate is ready.** `ITeamRepository` (create/get/update/delete team; add/remove/list/is member; `list_all_user_team_ids`) and `IOrganizationRepository` (create/get/update/delete org; add/remove member; **`update_member_role` / `get_member_role` / `user_has_permission`**). Concrete PG + Sessionless impls exist. RBAC: `Role` = ADMIN/MEMBER/VIEWER; permissions `org:manage_users`, `org:manage_settings`; `require_platform_admin` / `require_role(...)` deps in `api/middleware/auth.py`.
- **Key substrate asymmetry:** **org membership carries a per-member role; team membership has a `team_role` column but no update/query-for-authority path.** → v1 team management has no "team role" *authority* concept; authority to manage a team derives from the **org** role. (⚠️ Correction per §10-N3: `team_members.team_role` *does* exist — `models.py:466` — so per-team roles later are repo-method work, not a migration. U13b passes `team_role=None` consistently.)
- **Dashboard already has:** read-only `listTeams()` → `GET /api/v1/teams` (#44/#746), `useTeamSharing` (gated on the `team` scope from `/me/available-scopes`), `UserManagementPage` as the console template, and the anti-drift `access.ts` predicate + `useNavigationItems` pattern. There is **no org client** and **no "team admin" role** on the dashboard yet.

---

## 3. Proposed API surface (cloud, `faultmaven_cloud/api/`)

All routes org-scoped to the caller's tenant context; all mutating routes gated by `org:manage_users` (membership) / `org:manage_settings` (team/org settings). Idempotent where natural. Route-level integration tests required (the #746 lesson).

**Teams**
- `POST   /api/v1/admin/teams` — create team `{name, description?}` in caller's org.
- `PATCH  /api/v1/admin/teams/{team_id}` — rename / edit description.
- `DELETE /api/v1/admin/teams/{team_id}` — delete team (⚠️ §10-B4: delete is SOFT `deleted_at`; shares/members do NOT auto-cascade on soft delete, and `UniqueConstraint(organization_id, name)` has no `deleted_at` discriminator → delete-then-recreate same name = IntegrityError. U13b must decide cleanup + handle name reuse).
- `GET    /api/v1/admin/teams` — list all teams in the org (admin view; distinct from `GET /teams` = caller's own teams).
- `GET    /api/v1/admin/teams/{team_id}/members` — list members.
- `POST   /api/v1/admin/teams/{team_id}/members` — add existing org member `{user_id}`.
- `DELETE /api/v1/admin/teams/{team_id}/members/{user_id}` — remove member.

**Organization** (the container the team console sits in)
- `GET    /api/v1/admin/organization` — org detail (name, slug, member count).
- `PATCH  /api/v1/admin/organization` — edit org settings (`org:manage_settings`).
- `GET    /api/v1/admin/organization/members` — list org members + roles.
- `PATCH  /api/v1/admin/organization/members/{user_id}` — set role (ADMIN/MEMBER/VIEWER).
- `DELETE /api/v1/admin/organization/members/{user_id}` — remove from org.

**Invitations** (v1a — see §6; list/revoke DROPPED per §10-N4, no "pending" state in v1a)
- `POST   /api/v1/admin/organization/members` — add existing enterprise user `{email|username, role}` (resolves the user within the caller's enterprise, then adds to the org). This IS the v1a "invite". ⚠️ §10-gap: needs the core `enterprise_id` mapping fix (`user_repository.py:308`) + an enterprise-scoped lookup, else cross-enterprise adds are possible.
- ~~`GET/DELETE .../invitations`~~ — deferred to v1b (tokenized email); not built in v1a.

Note the `/admin/*` prefix keeps these clearly separate from the open-core read-only `GET /teams`. They mount only when the cloud app runs.

---

## 4. The composition-root seam (the enabling prerequisite)

The console can't be wired until cloud can mount routes onto the app. Two options:

- **Option A (recommended): cloud composition root imports the core app.** Create `faultmaven_cloud/main.py` that does `from faultmaven.main import app`, then `app.include_router(...)` for the cloud admin routers, and re-exports `app`. This satisfies the existing (dangling) `Dockerfile` CMD `faultmaven_cloud.main:app`, needs **zero core change**, and honors the sealed-container rule (routers attach to the app object post-import; DI overrides still go through the env-driven factory branches, unchanged). Matches the cloud README's intent ("mount cloud-only routes after the app is created").
- **Option B: add a `create_app()` factory to core.** Cleaner in theory (no import-time singleton) but a **large core refactor** (every import-time `app.include_router` and the lifespan move into a factory), touching the most load-bearing file. Higher risk, out of proportion to U13.

→ **Recommend A.** It's the smallest change that unblocks *all* cloud-only routes (not just U13), and it doubles as the fix for the dangling Dockerfile entrypoint. (This also subsumes part of U16.)

**DI note:** the management repos are core Protocols selected by the tenant provider. The console consumes them via the same request-scoped resolution the read path uses; nothing new in the container. `create_team_service()`/tenant provider stay the single wiring gate — the console is simply `None`-guarded and returns 503 "not available" until `MULTI_TENANT_READY`.

---

## 5. RBAC & gating

- **Authority model (v1):** org role governs everything. `ADMIN` (has `org:manage_users` + `org:manage_settings`) may manage teams, members, and invitations within their org. `MEMBER`/`VIEWER` cannot. No separate "team admin" role in v1. ⚠️ **§10-B2:** enforcement is NOT `require_role(Role.ADMIN)` — JWT roles come from `users.dev_roles` (platform/system admin), not the RBAC `organization_members→roles` path (#706), so `require_role` tests the wrong authority. v1 authority check = `IOrganizationRepository.get_member_role` / `user_has_permission` against the request-resolved org. ⚠️ format trap: `user_has_permission` parses `'resource.action'` (DOT), the Permission enum uses `'org:manage_users'` (COLON) — map explicitly or it silently returns `False`. `require_role` may remain only as a coarse outer guard.
- **Tenant isolation:** every action is org-scoped via the request tenant context (ADR-010 P2). ⚠️ **§10-B1:** RLS on `teams` covers team-object writes (update/delete of a foreign-org team → rowcount 0), but `team_members` is NOT RLS-tenanted and `add_member`/`remove_member`/`list_team_members` do NOT join through `teams`. So U13b MUST `get_team(team_id)` under RLS context first (foreign org → 404) AND verify the target `{user_id}` is a member of the caller's org (`get_member_role`) before any membership mutation — both as tested invariants. RLS alone does NOT cover the membership boundary.
- **Dashboard gate:** a new `access.ts` predicate `canManageOrg(deployment, role)` = cloud + platform_admin/org-admin, consumed by both the route guard and `useNavigationItems` (anti-drift). Visibility additionally gated on a capability signal so the console is hidden in standalone and pre-P2 cloud (see §7).

---

## 6. Invitations — minimal v1

Two viable shapes:
- **v1a (recommended, no email dependency): invite-by-existing-user.** "Add member" resolves an existing user by email/username within the enterprise and adds them to the org/team directly. Pending "invitations" table optional. Zero email infra; works the day P2 lands.
- **v1b: tokenized email invite.** Generate a signed invite token, email a join link, user accepts → joins org. Needs email delivery + an `invitations` table + accept endpoint + token expiry. More product surface; defer unless required for launch.

→ **Recommend v1a for U13**, with the `invitations` endpoints (§3) stubbed to the add-existing-user path; upgrade to v1b as a later cloud unit if self-serve email invites are a launch requirement. **(Decision for sign-off.)**

---

## 7. Inert-until-P2 posture (how nothing lights up early)

- Cloud management routes mount only in the cloud app; standalone never sees them.
- Even in cloud, until `MULTI_TENANT_READY` flips, `create_team_service()` is `None` and the routes return 503 "management not available" (fail-closed, tested).
- Dashboard console hidden unless a capability says team management is live. **Option:** extend the capabilities/`available-scopes` signal, or add a small `managementConsole` capability, set true only when multi-tenant is active. (Reuses the `useTeamSharing` gating pattern.) **(Minor decision for sign-off.)**

---

## 8. Proposed PR sequencing (post-sign-off, one unit/PR)

1. **U13a [cloud + core-adjacent]** — composition root: `faultmaven_cloud/main.py` importing core `app` + a smoke test that the cloud app boots and mounts a trivial cloud router; fixes the dangling Dockerfile entrypoint. Unblocks all cloud routes. (Subsumes part of U16.)
2. **U13b [cloud]** — Team management API (create/edit/delete team + membership) + `TeamManagementService` driving `ITeamRepository`; route-level integration tests; 503 when `team_service` None.
3. **U13c [cloud]** — Organization admin API (org detail/settings + org membership + roles) driving `IOrganizationRepository`; invitations v1a.
4. **U13d [dashboard]** — Org/Team console page (model on `UserManagementPage`) + `canManageOrg` predicate + nav item + org/team admin clients; capability-gated. Blocked on U13b/c contracts.

Each lands **inert** (cloud not multi-tenant-ready), matching the Wave 4 pattern; runtime activation is ADR-010 P2.

---

## 9. Decisions — SIGNED OFF (owner, 2026-07-20)

All five decisions approved as recommended.

1. **Composition root** — **Option A** (import core `app` in `faultmaven_cloud/main.py`). ✅ *Implemented in U13a (faultmaven-cloud PR #7).*
2. **Invitations** — **v1a** (add existing enterprise user, no email). v1b (tokenized email) is a later cloud unit if self-serve email invites become a launch requirement.
3. **Role granularity** — **org-role-only authority** for v1 (no per-team role). Per-team roles = future substrate + migration change, out of scope.
4. **Console gate signal** — **dedicated `managementConsole` capability flag**, set true only when multi-tenant is active (mirrors the `teamSharing`/`useTeamSharing` gating pattern).
5. **Scope** — **as scoped in §1**: org admin included (not team-only); billing/metering, SSO/SCIM, enterprise-tier admin, cross-org platform admin all excluded.

---

## 10. Fable pressure-test (2026-07-20) — verdict: SIGN WITH FIXES

Adversarial design pressure-test against `origin/main` of both repos (local checkouts were stale — verified against origin). The seam (U13a) and the `teams`-table substrate are solid; the fixes below are doc corrections + explicit substrate work that must land **before/inside U13b/U13c** — no redesign. Inline ⚠️ pointers in §2/§3/§5 reference these.

**Verified solid:** no `create_app()`, so U13a's `from faultmaven.main import app` is import-safe (container seals in lifespan at startup, not import — `main.py:867`, `:332`); `MULTI_TENANT_READY=False` raises at both the tenancy factory (`providers/tenancy/factory.py:108`) and coherence gate (`config/deployment_coherence.py:139`); `create_team_service()` → `None` pre-P2 (`container/providers/services.py:349`); interfaces exist as listed; `teams` is RLS-tenanted (migration `018:49`) so team-object writes fail closed; no `/api/v1/admin/*` collisions; dashboard substrate (`useTeamSharing`, `access.ts`, `GET /teams`) confirmed.

**Blocking (fix before U13b/c):**
- **B1** — membership writes are NOT covered by RLS (`team_members` untenanted; `team_repository.py:145–202` has no `teams` join). → U13b `get_team()` under RLS + verify target's org membership, tested. (§5 corrected.)
- **B2** — `require_role(Role.ADMIN)` tests platform admin, not org admin (JWT roles = `users.dev_roles`, #706). Use `IOrganizationRepository.get_member_role`/`user_has_permission`; mind the dot-vs-colon permission-string trap (`organization_repository.py:260`). (§5 corrected.)
- **B3** — `roles`/`permissions`/`role_permissions` tables are **empty** (no seed anywhere; the "baseline seeds RBAC" note is stale). `organization_members.role_id` is `NOT NULL RESTRICT` → no org member can be added, and role-setting/invitations are unimplementable until a **core seed migration** (from `models/rbac.py ROLE_PERMISSIONS`) lands. Repo `add_member` takes a `role_id` UUID, not an enum — needs a name→id layer. **→ U13c requires CORE PRs (seed migration = HEAD_REVISION bump + migration-integration suite); it is NOT purely "[cloud]" per §8.**
- **B4** — team delete is SOFT; share/member cascade only runs on HARD delete (0 prod callers); `UniqueConstraint(org_id, name)` lacks a `deleted_at` discriminator → recreate-same-name = 500. → U13b: partial unique index `WHERE deleted_at IS NULL` (or rename-on-delete) + explicit cleanup decision. (§3 corrected.)

**Gaps / risks:**
- **N1/N2 (console gate) — RESOLVED (owner-delegated, principled, 2026-07-20): core emits `managementConsole` from the `team_service` signal (Option A).** Rationale: the flag is a capability *advertisement*, not the proprietary feature (the console code stays in cloud per ADR-010 D7); FaultMaven already centralizes advertisement in core `/v1/meta/capabilities` and `teamSharing` is already core-emitted → consistency (Principle 3); and core must never depend on cloud (Option B's core-reads-a-cloud-marker inverts the dependency, FSL core vs proprietary cloud). Predicate = `app.state.team_service is not None` (NOT `deployment_mode=="cloud"`). **Root-cause add:** `teamSharing` currently keys on `deployment_mode=="cloud"` (`main.py:1637`) so it *also* lights pre-P2 — correct it to the same `team_service is not None` signal in the same change (Principle 1). Lands as a core capabilities-emitter change (with a `/v1/meta/capabilities` endpoint test per #746), sequenced so U13d can gate on it.
- **Invitation lookup (Decision #2):** `get_by_email`/`get_by_username` are GLOBAL, `users` is untenanted, and `_model_to_domain` doesn't map `enterprise_id` (`user_repository.py:308`) → "within the enterprise" can't be enforced without a core one-line fix; without it, cross-enterprise adds are possible. Also `add_member` can't record `invited_by` (interface takes only `(org_id, user_id, role_id)`). Another CORE touch in U13c.
- **N4 — RESOLVED (owner-delegated, principled, 2026-07-20): DROP §3's invitation list/revoke endpoints for v1.** v1a "invite" IS just `POST /admin/organization/members` (add existing enterprise user) — no "pending" state exists, so nothing to list/revoke. A 501 stub is dead API surface ([[no_dead_code_verify_liveness]]) that leaks into OpenAPI/generated clients, and reserving a slot for unbuilt v1b violates the pre-data clean-baseline rule ([[no_backcompat_pre_data]]). The endpoints return, designed for the real shape (token/expiry/accept), only if/when v1b lands. → §3 "Invitations" block reduces to the add-existing-user path under org membership.
- **N3** — `team_members.team_role` exists (`models.py:466`); §2 corrected. v1 passes `team_role=None`.

**Sequencing note:** U13c is cross-repo (core seed migration + `enterprise_id` mapping + possibly `add_member` signature) — §8's "[cloud]" label understates it. B1's get-team-first invariant + N1's `team_service`-None guard signal belong in U13b's acceptance criteria.
