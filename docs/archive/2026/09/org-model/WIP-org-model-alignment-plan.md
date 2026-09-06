# WIP — Org-Model Alignment Campaign (Phase 3)

**Basis:** ADR-013 (canonical Enterprise / Organization / Team model) + `enterprise/multi-tenancy.md` + glossary — merged 2026-07-18 (doc-internal PR #24 reconciled + #25). This is the execution plan to align docs, schema, and code to those definitions.

**Operating model:** one PR-sized unit per session, tests + review per unit, isolated worktrees (shared-tree hazard), docs → schema → code, dependency-ordered. Each unit tagged **[consistency]** (align existing to ADR-013 / remove drift) or **[feature]** (build the team-sharing mechanism ADR-013 defines — the resumed KB-team-sharing project). Repo in brackets.

---

## ⚠️ STATUS 2026-07-26 — all sixteen units are built; the campaign is build-complete

Verified unit-by-unit against `origin/main` in all six repos (faultmaven `a47e9eed`,
faultmaven-cloud `13f41a2`, dashboard `d25b25d`, slack-agent `31a3957`, copilot,
doc-internal `09708a9`). **What is left is activation, not construction** — see
"Remaining" below. Do not re-scope any U1–U16 unit from the wave sections; they
are kept as the historical record of what each unit covered.

### Remaining (in dependency order)

1. **Nothing turns multi-tenancy on — faultmaven#629.** The old
   `MULTI_TENANT_READY = False` hard gate is **gone**; `providers/tenancy/factory.py`
   now boots `multi`, failing closed only when `DEPLOYMENT_MODE != cloud`. But
   `TENANT_PROVIDER` is set **nowhere** in `faultmaven-enterprise-infra`, so cloud
   runs `single`, `create_team_service()` returns `None`, and the entire team layer
   is inert at runtime despite being built. Per `project_org_model_consistency`, the
   flip is additionally blocked on the **unbuilt WorkOS Org→FM-org SSO mapping**.
   **This is the gate for everything below.**
2. **The cloud admin API is built but not deployed.**
   `faultmaven-enterprise-infra/available-images/README.md` records the image as
   "Cloud Edition (registered, not deployed)" and `promote-images.yaml` explicitly
   skips it. Since `/api/v1/admin/teams` + `/admin/organization` are the **only**
   membership-population path, `team_members` has no live writer outside the
   standalone default-team row seed. (Both core prerequisites are satisfied — RBAC
   seeding by migration 029, `enterprise_id` mapping at `user_repository.py:336` —
   so this is a deploy, not a build. Cloud issue #9 closed 2026-07-26.)
3. **U15 — Slack workspace→Team binding is unbuilt (slack-agent#25, open).**
   `CaseService._auto_share_slack_case` shares to "every Team the workspace service
   account belongs to", but nothing binds a Slack workspace to a Team or adds the
   service account to `team_members`; `scripts/auth/provision_service_account.py`
   has no team support and no binding endpoint exists in core or cloud. So ADR-013
   D3's Slack half is wired to resolve to the **empty set**. Beta runs all
   workspaces under one service token (slack-agent `docs/design.md` §15.2/15.3).
4. **ADR-013 is still `Status: Proposed`** (as are ADR-011 and ADR-012) — the whole
   campaign executed against an unaccepted ADR. Two of its four "Open items" were
   in fact settled by this work and the doc does not record it: **item 2**
   (`team_members` RLS posture) by **migration 030**, and **item 1** (RBAC role set)
   by **migration 029**. Advancing 013 to Accepted is an owner call.

**Deliberately not built, per ADR-013 itself — these are not gaps:** org-level
sharing (D4a — the `resource_shares.scope_type` CHECK already admits
`'organization'`, so it is a row, not a migration) and ADR-011 D5 case-collaboration
(`case_collaborators`, per-turn authorship).

---

## Wave 1 — Docs alignment  [consistency] — **DONE**

- **U1 [doc-internal] — DONE.** Stale tenancy docs aligned to ADR-013; the missing authority docs now exist and cite it: `architecture/enterprise/multi-tenancy.md`, `security/architecture/tenant-isolation.md` ("Crown Jewels"), plus `enterprise/README.md`, `deployment/cloud/multi-tenancy/*`, `deployment-agnostic-architecture.md`, `dashboard-access-control-design.md`, `guides/glossary.md`.
- **U2 [slack-agent] — DONE (PR #33).** `workspace → Team (within the customer's Org)` vocabulary alignment in docs/comments.

## Wave 2 — Backend naming hygiene  [consistency] — **DONE**

- **U3 [faultmaven] — DONE.** "Workspace" = Organization mislabel retired; the only surviving `workspace` mention in `models.py` (:314) is a legitimate Slack-workspace reference.
- **U4 [faultmaven] — DONE.** Duplicate models consolidated: one `ITeamRepository` (`models/interfaces_user.py:444`), one `KnowledgeScope`, one `Organization` Pydantic (`interfaces_user.py:140`). `modules/auth/domain/models/organization.py` is deleted. → **faultmaven#519 was the stale tracker for this; closed 2026-07-26.**
- **U5 [faultmaven + cloud] — DONE.** Plan-tier collision renamed `enterprise` → `business` (migration 026).

## Wave 3 — Scope-model consistency  [consistency] — **DONE**

- **U6 [faultmaven] — DONE.** Orphaned `organization` KB scope removed (migration 027); `KnowledgeScope = PERSONAL | TEAM | GLOBAL`. The remaining `"organization"` string literals in code are `RoleScope` (RBAC role level) — a different, legitimate concept.

## Wave 4 — Team-sharing FOUNDATION  [feature] — **COMPLETE**

- **U7 [faultmaven] — DONE (faultmaven#734).** Concrete `TeamRepository` + `SessionlessTeamRepository` (`list_all_user_team_ids` via join-through-`teams`), `TeamService`, DI wiring (`create_team_service` → `container.team_service` → `app.state.team_service`), default Team seeded in `single_tenant.py`. `team_members` RLS posture decided → **migration 030**.
- **U8 [faultmaven] — DONE (PR #740).** Polymorphic `resource_shares` (`resource_type, resource_id, scope_type, scope_id`; v1 `scope_type=team`, `organization` reserved for D4a) + clean drop of the three nullable `team_id` columns (no backfill — they never had a live writer). Absorbed the KB half of U9: `build_kb_scope_filter` is now the visible-id allowlist (global ∪ owned ∪ `parent_document_id ∈ shared-to-my-teams`); the drifted `kb_qa._build_scope_filter` and dead `resolve_accessible_scopes` deleted; ChromaDB metadata never carries team state (unshare-trap safe).
- **U9 [faultmaven] — DONE (faultmaven#751).** Case-side visible-id allowlist: `CaseService._resolve_shared_case_ids` (:789) behind the single gateway at :772; seeder pre-fetch owner-team arm (`milestone_engine.py:9082`, keyed on the **case owner**); KB QA (`agent_orchestration_service.py:451`); `/me/available-scopes` (`auth.py:897`).
- **U10 [faultmaven] — DONE (PR #743, faultmaven#744).** Cases shareable to teams + D3 share defaults: `_share_case_with_team`, `_auto_share_slack_case` wired into `create_case`, share cascade on `hard_delete_case`.

## Wave 5 — Frontend alignment — **DONE**

- **U11 [dashboard] — DONE.** `PublishableScope = 'personal' | 'team' | 'global'`; dead `KBTabs` removed; scope types unified to 3-tier.
- **U12 [dashboard] — DONE.** Team in case views: `ShareCaseModal.tsx`, `TeamShareBadge.tsx`, `useTeamSharing`, share info on the case DTO.
- **U13 [dashboard + cloud] — DONE.** Cloud composition root `faultmaven_cloud/main.py` (Option A — imports the core app, fixing the dangling Dockerfile entrypoint) + `api/admin/teams.py` (U13b, PR #10) + `api/admin/organization.py` incl. v1a invitations (U13c, PR #12) + `services/team_management.py` / `organization_management.py`. Dashboard console: `console/TeamsPanel.tsx`, `OrganizationPanel.tsx`, `InviteMemberModal.tsx`, capability-gated via `useCapabilities`. Design: `WIP-u13-org-team-management-console-design.md`.
- **U14 [copilot] — DONE.** `teamWorkspaces` → `teamSharing` (the misnomer is now documented against ADR-013 in `src/lib/capabilities.ts`); KB scope model aligned.

## Wave 6 — Slack-agent + cloud behavior

- **U15 [slack-agent] — NOT BUILT (slack-agent#25, open).** See "Remaining" #3. Blocked on the backend asks in slack-agent `docs/design.md` §15.2 (workspace→Team binding API) and §15.3 (first-class service-identity token type), and P2-gated behind faultmaven#629.
- **U16 [cloud] — DONE (PR #13).** The phantom `multi_tenant_user_repo` reference was dropped and the CE user-repo substrate documented.

---

## Sequencing notes

- **Waves 1–3 (consistency) first** — cheap, low-risk, and they clean the ground (remove the phantom scope, the mislabels, the duplicate models) before the feature build touches the same surfaces.
- **Wave 4 is the gate for the KB-team-sharing feature** and for the paused KB-seeder team arm — the seeder consumes team-scoped runbooks only once U7–U10 land. **KB-seeder R3** (provenance-uniqueness) is independent and can run anytime in parallel.
- **Multi-tenant readiness (ADR-010 P2)** gates cloud *activation* of Wave 4/U13; the code lands core-side and stays inert in standalone until then. **This is now the only thing standing between the built team layer and observable behavior — see "Remaining" #1.**
- Open items carried from ADR-013: `team_members` RLS posture (**settled by migration 030**), verify-authorization + team-verified trust tier (**still open** — KB-seeder work), RBAC role set (**settled by migration 029**).
