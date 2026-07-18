# ADR-002: Team Collaboration Layer

**Date**: 2026-07-18
**Status**: Proposed
**Decision Makers**: Engineering Leadership, Product Team
**Affects**: Tenancy, Auth/RBAC, Knowledge sharing (KB scopes), Cloud offering, Slack integration

---

## Context

FaultMaven's data model already defines a three-level tenancy hierarchy —
**Enterprise > Organization > Team** — and ships the tables for it: `teams`
(`organization_id` FK, `ON DELETE CASCADE`; unique `(organization_id, name)`)
and `team_members` (composite PK `(user_id, team_id)`, `team_role`). Domain
entities `Team` and `TeamMember` exist, and `cases.team_id` is a real column.

Everything **above the data model is unbuilt**:

- No concrete `TeamRepository` — only the `ITeamRepository` ABC;
  `list_all_user_team_ids(user_id)` is a declaration with no implementation.
- No `TeamService`.
- No DI wiring — `team_service` is hard-coded to `None` at the single
  construction site (`api/dependencies.py`, with the comment "standalone
  deployment has no team collaboration").
- No Team management API — so `teams` / `team_members` are **never populated at
  runtime**.
- KB team-scope **write** plumbing exists (`ingest_runbook(team_id=…)` →
  `VectorMetadata`), but `team_id` is **unvalidated free-form** (no membership
  or FK check), and the **read** side always resolves `team_ids = []` because no
  team service is wired.
- No Slack-workspace ↔ team mapping exists in code.

The consequence surfaced concretely in the KB cause-seeder flywheel (see
[`kb-cause-seeder.md`](../knowledge-and-ai/kb-cause-seeder.md)): the KB QA
retrieval path and the seeder pre-fetch both build a scope filter over
`global ∪ personal ∪ team`, but the team branch is dead — nothing can resolve a
principal's teams, so team-scoped knowledge is never readable. The same is true
for the whole KB read path.

**Why this matters.** Team — not Organization — is the *natural sharing unit*
for troubleshooting knowledge: a team owns a service, runs its incidents, and
accumulates the runbooks that resolve them. **Team-scoped sharing is the core
value the Cloud offering adds over the free Standalone deployment.** Without a
functioning team layer, Cloud has no differentiator, and the knowledge flywheel
can only ever be per-user or globally-published — never "my team's resolved
incidents help my teammates."

The current inert state is therefore **incomplete implementation, not a design
boundary**. This ADR decides to complete it.

This ADR relates to two decisions that live as concepts in code comments and
notes but are not yet committed ADRs in this repo: the tenant-provider seam
(`TENANT_PROVIDER = single | multi`, described in
[`architectural-design-principles.md`](../core-architecture/architectural-design-principles.md))
and the identity/principals/RBAC direction (team- vs user-originated cases). It
depends on the former and supplies the membership substrate the latter needs.

---

## Decision

Build the **team collaboration layer** as a first-class, integrated part of the
system, scoped as a **Cloud (multi-tenant) feature** gated behind the tenant
provider. Standalone remains single-user / single-organization and resolves an
empty team set by design.

The layer is built in dependency order (see Implementation Strategy). The
load-bearing decisions:

1. **Hierarchy.** A team is a sub-unit of an organization (Enterprise >
   Organization > Team, already the schema). A user may belong to many teams
   within their organization. `team_members` is the single source of truth for
   membership; `team_role` carries the member's role within the team.

2. **Cloud-only, behind the tenant provider.** Team resolution and team-scoped
   KB sharing activate only under `TENANT_PROVIDER = multi`. Under `single`
   (Standalone), the team service resolves `[]` and team scope is inert — no
   dead code, just a provider that answers "no teams" honestly. This mirrors how
   organization resolution is already gated by the tenant provider.

3. **Membership is populated, not assumed.** Read-side resolution is worthless
   until `team_members` has rows. Membership is written through **two sources,
   both landing in `team_members`**: (a) a **Team management API** (dashboard —
   create team, add/remove member, list) as the canonical CRUD path, and (b) a
   **Slack-workspace sync** for Slack-originated tenants, where a Slack workspace
   maps to a team and workspace membership projects onto `team_members`. (The
   Slack mapping model is an open question — see below.)

4. **Team-scoped KB writes are membership-validated.** Publishing a KB item with
   `scope="team", team_id=T` requires the caller to be a member of `T` (checked
   against `team_members`), replacing today's unvalidated free-form `team_id`.
   This makes team scoping trustworthy before anything reads it.

5. **Read resolution is keyed on the right principal.** For KB QA the principal
   is the requesting user; for the **cause-seeder pre-fetch it is the case
   owner** (`case.user_id` / `case.team_id`), not the session user, so a case
   seeds from the teams its owner belongs to. Both paths converge on the shared
   `build_kb_scope_filter(owner_id, team_ids)` helper introduced in R2 — team
   OR-extends the filter the moment `team_ids` resolves non-empty, so no
   retrieval-site changes are needed beyond passing the resolved ids.

6. **RBAC.** `team_role` on `team_members` carries the role. The concrete role
   set (e.g. member / lead / admin) and the permissions each grants are settled
   with the identity/RBAC direction; this ADR requires only that the repository
   expose membership and role so RBAC can build on it.

---

## Implementation Strategy

One PR-sized unit per phase, tests required, following the campaign's operating
model (isolated worktree, `lint-imports`, `/code-review`).

### Phase 1 — Membership resolution (repository)

Concrete `TeamRepository` implementing `ITeamRepository`, modeled on
`OrganizationRepository` (`PostgreSQL…` + `Sessionless…` variants).
`list_all_user_team_ids(user_id)` is `SELECT team_id FROM team_members WHERE
user_id = :uid`. Unit-tested against the real tables. **No behavior change yet**
(nothing constructs it) — pure substrate.

### Phase 2 — Wiring, cloud-gated

Construct the team service/repository in the DI container and place it on
`app.state` (mirroring `tenant_provider`), **only under `TENANT_PROVIDER =
multi`**; Standalone wires `None`. Thread it to the two existing consumers that
already accept `team_service` (`agent_orchestration_service`, the knowledge
route) so they stop hard-coding `None`. Still no populated memberships → still
inert, but now the resolver is live.

### Phase 3 — Team management API (population path)

Endpoints to create a team, add/remove members, and list a user's teams —
RBAC-gated, organization-scoped. This is the first path that populates
`team_members`. Dashboard consumes it.

### Phase 4 — Team-scoped KB write validation

`/knowledge/convert`, `/runbooks/create`, `/knowledge/scan`, and the conversion
funnel validate `team_id` against the caller's memberships; reject a team the
caller is not in. Closes the "any authed user can publish to an arbitrary
team_id" hole.

### Phase 5 — Read-side resolution wired end-to-end

Resolve `team_ids` in the KB QA path (requesting user) and the cause-seeder
pre-fetch (**case owner**), pass them into `build_kb_scope_filter`. This is the
step that actually makes team-scoped knowledge readable and closes the team arm
of the flywheel loop. Cross-tenant isolation tests: a user only ever resolves
their own teams; a case only ever surfaces its owner's teams.

### Phase 6 — Slack workspace ↔ team mapping

Map a Slack workspace to a team and sync workspace membership into
`team_members`, coordinated with the Slack agent. (Design in a follow-on once
the mapping model is settled.)

### Phase 7 — Cleanup

Remove the dangling `TeamRepository` export in
`modules/auth/infrastructure/repositories/__init__.py`; reconcile CLAUDE.md,
which advertises team files (`auth/api/teams.py`, `team_service.py`) that will
now actually exist.

---

## Consequences

### Positive

- Cloud gains its defining capability: teams share resolved-incident knowledge,
  and the flywheel compounds across a team, not just an individual.
- One isolation seam. `build_kb_scope_filter` is the single source of truth for
  KB read scoping across QA and seeding; team is an additive OR-branch, so the
  cross-user isolation already tested for personal extends to team by
  construction.
- Team scoping becomes trustworthy (membership-validated writes) before it is
  ever read.
- Standalone is unaffected — the tenant provider answers "no teams," and the
  code path is the same one exercised today.

### Negative

- New surface to build and test (repository, service, wiring, management API,
  Slack sync) across auth, knowledge, and API layers.
- Multi-tenant test coverage is currently thin; team paths need new fixtures
  (organizations with multiple members and teams).
- Slack-workspace mapping introduces an external source of truth that must stay
  reconciled with `team_members`.

### Mitigation

- Phase gating: each phase is independently shippable and inert until the next
  activates it, so partial delivery never regresses Standalone or breaks reads.
- Cloud-only gating via the tenant provider keeps Standalone on the exact code
  path it runs today.
- Membership-validated writes land before read resolution, so no untrusted
  team-scoped item is ever seeded.

---

## Alternatives Considered

- **Organization-level sharing only (drop teams).** Rejected: organizations are
  too coarse — the whole company is not the sharing unit; a team that owns a
  service is. It also contradicts the existing schema and the Slack-workspace
  model.
- **Team as a flat tag on KB items (no membership).** Rejected: without a
  membership source of truth, "team" scope cannot be authorized on write or
  resolved on read — it degrades to an unvalidated label, exactly today's hole.
- **Build the team layer in `faultmaven-cloud` instead of core.** Rejected: the
  schema, KB scope model, and retrieval seam all live in core; splitting the
  layer across repos would fragment the isolation invariant. Core carries the
  inert-by-default multi-tenant seam (per the tenant-provider decision); teams
  belong with it.

---

## Open Questions (to resolve before / during the phases)

1. **Membership source of truth precedence.** When both the management API and
   Slack sync can write `team_members`, which wins on conflict? Is Slack
   authoritative for Slack-originated tenants, with the API for the rest?
2. **Slack mapping cardinality.** Workspace ↔ team 1:1, or does a workspace map
   to an organization with channels/user-groups as teams?
3. **RBAC role set.** The concrete `team_role` values and their permissions
   (settled with the identity/RBAC direction).
4. **Standalone teams.** Confirmed out of scope here (Cloud-only). Revisit only
   if a self-hosted multi-user org needs local teams.

---

## References

- [KB Cause Seeder](../knowledge-and-ai/kb-cause-seeder.md) — the flywheel loop
  whose team arm this unblocks; `build_kb_scope_filter` read seam.
- [Document → Runbook Conversion](../knowledge-and-ai/document-to-runbook-conversion.md)
  — produce-side KB scoping.
- [Architectural Design Principles](../core-architecture/architectural-design-principles.md)
  — `TENANT_PROVIDER` seam, tenancy tables.
- [IAM Design](../security/iam-design.md) — organization context and claims.

---

**Last Updated**: 2026-07-18 (Proposed — awaiting review)
