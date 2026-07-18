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

   **Hard prerequisite — multi-tenant readiness.** `TENANT_PROVIDER = multi`
   **cannot boot today**: `providers/tenancy/factory.py` sets
   `MULTI_TENANT_READY = False` and `create_tenant_provider` fails *closed* on
   `multi` (raises `TenancyConfigurationError`) because request→organization
   wiring and full row-level isolation have not shipped. So every Cloud
   deployment currently runs `single`. Team **Phase 2 onward therefore depends on
   multi-tenant readiness landing first** (the ADR-010 forward-consolidation P2
   work: request→org wiring + flipping `MULTI_TENANT_READY` + the RLS posture in
   the isolation subsection below). Phase 1 (the repository) is pure substrate and
   can land before that; Phases 2–5 cannot be activated or end-to-end tested until
   multi is bootable. This dependency, not team code, is the true gate on the
   Cloud feature going live.

3. **Membership is populated, not assumed.** Read-side resolution is worthless
   until `team_members` has rows. Membership is written through **two sources,
   both landing in `team_members`**: (a) a **Team management API** (dashboard —
   create team, add/remove member, list) as the canonical CRUD path, and (b) a
   **Slack-workspace sync** for Slack-originated tenants, where a Slack workspace
   maps to a team and workspace membership projects onto `team_members`. (The
   Slack mapping model is an open question — see below.)

4. **Team-scoped KB writes AND verification are membership-validated.** Publishing
   a KB item with `scope="team", team_id=T` requires the caller to be a member of
   `T` (checked against `team_members`), replacing today's unvalidated free-form
   `team_id`. Critically, the **seedability boundary is verification, not
   ingestion**: `get_runbook_causes` refuses `EXPERIMENTAL` items, so it is the
   `verify_draft` / `verify_batch` promotion to `COMMUNITY` that actually makes an
   LLM-authored team runbook seedable to every teammate. Those verify endpoints
   are **auth-only today** (no scope or membership check). So membership
   validation must cover the verify path, not just create/convert — it is the
   real trust gate for team seeding (see Phase 4).

5. **Read resolution is keyed on the right principal.** For KB QA the principal
   is the requesting user; for the **cause-seeder pre-fetch it is the case
   owner**, not the session user. *Which* of the owner's scopes seeds is an open
   question (see below): all teams the owner belongs to, versus only the case's
   own team (`cases.team_id` exists) — these diverge for team-originated cases.
   Both paths converge on the shared `build_kb_scope_filter(owner_id, team_ids)`
   helper introduced in R2 — team OR-extends the filter the moment `team_ids`
   resolves non-empty, so no retrieval-site changes are needed beyond passing the
   resolved ids.

6. **RBAC.** `team_role` on `team_members` carries the role. The concrete role
   set (e.g. member / lead / admin) and the permissions each grants are settled
   with the identity/RBAC direction; this ADR requires only that the repository
   expose membership and role so RBAC can build on it.

### Isolation and RLS (the substrate the OR-filter trusts)

KB reads resolve against **ChromaDB**, which PostgreSQL RLS never sees — so
`build_kb_scope_filter` is the *only* isolation on the KB read path, and its
`team_ids` input is therefore security-critical. A single wrong `team_id` in that
list serves another org's entire team KB. Two facts make this sharp:

- `team_members` (composite PK `(user_id, team_id)`, no `organization_id`) is
  **not** in the RLS `_TENANTED_TABLES` set (migration 018) and structurally
  cannot be org-tenanted as it stands; only `teams` carries an RLS policy.
- Nothing today constrains a `team_members` row to link a user and a team of the
  **same organization** — the future writers (Phase 3 API, Phase 6 Slack sync)
  are the only defense.

Decisions this ADR fixes:

- **Same-org membership invariant.** A `team_members` row may link a user and a
  team only within one organization. Enforced by every writer (Phase 3, Phase 6).
- **Resolution joins through `teams`.** Membership resolution is
  `SELECT tm.team_id FROM team_members tm JOIN teams t ON t.team_id = tm.team_id
  WHERE tm.user_id = :uid`, run under the **limited `faultmaven_app` role** (RLS
  is bypassed for owner/superuser). The join makes the existing `teams` RLS policy
  filter cross-org memberships **fail-closed for free**, defending the OR-filter's
  input even if a bad membership row exists.
- **`team_members` RLS is an open question** (org column + policy, or rely on the
  join) — see below; it is the one place this layer may need a new migration.

---

## Implementation Strategy

One PR-sized unit per phase, tests required, following the campaign's operating
model (isolated worktree, `lint-imports`, `/code-review`).

### Phase 1 — Membership resolution (repository)

Concrete `TeamRepository` implementing the auth-module `ITeamRepository`
(`modules/auth/domain/models/organization.py` — the ABC that declares
`list_all_user_team_ids`, the method both consumers call; the second ABC in
`models/interfaces_user.py` lacks it and should be consolidated), modeled on
`OrganizationRepository` (`PostgreSQL…` + `Sessionless…` variants).
`list_all_user_team_ids(user_id)` **joins through `teams`** (see Isolation and
RLS) so cross-org memberships fail-closed, and runs under `faultmaven_app`.
Unit-tested against the real tables. **No behavior change yet** (nothing
constructs it) — pure substrate.

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

`/knowledge/convert`, `/runbooks/create`, `/knowledge/scan`, and — explicitly —
the **`verify_draft` / `verify_batch`** promotion validate `team_id` against the
caller's memberships; reject a team the caller is not in. Verify is called out
because it is the EXPERIMENTAL→COMMUNITY promotion that makes a team draft
seedable (Decision 4); leaving it out would let a non-member promote a
team-scoped draft into the seedable tier — the NO-INCORRECT-CONCLUSION surface
for teams. Whether verify additionally requires a `team_role` (not just
membership) is deferred to the RBAC open question. Closes the "any authed user
can publish/verify to an arbitrary team_id" hole.

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

> The "Phase N" labels here are this ADR's own build phases, distinct from the
> KB-remediation campaign's numbered phases (see next section).

---

## Relationship to the KB-remediation campaign

This ADR arose from the KB-remediation campaign (the KB-pipeline overhaul that
built the cause-seeder and is closing the produce↔consume flywheel). It
**inserts a parallel track; it does not reorder or remove any campaign
objective.** The campaign's goal — structured KB actually consumed by the engine,
the produce↔consume flywheel closed, the pipeline hardened under the two
soundness guarantees — is fully achievable independent of teams; the team layer
adds the team-sharing dimension on top.

**Orthogonality.** The campaign's remaining work is consume-side depth (seeded
causes carrying indicators/interventions into validation/solution), produce-side
trust (who may publish what; not laundering failed fixes), and engine/ingest
robustness. The team layer touches a separate concern: the **retrieval scope** of
the KB read path — *which* runbooks are visible — which R2 already generalized
behind `build_kb_scope_filter`. No remaining campaign unit depends on team
membership:

| Remaining campaign unit | Concern | Depends on team? |
|---|---|---|
| R3 — provenance-uniqueness offer gate | produce-side offer | No — keys on seed provenance, scope-agnostic |
| R4 — global verify/scan admin gate | produce-side write-auth | No — same family as team Phase 4, complementary |
| R5 — solution-outcome annotation | produce-side content trust | No |
| R6 — ignored-seed decay (#713) | engine housekeeping | No |
| R7 — latent/quality | ingest/retrieval hygiene | No |
| R8 / R9 — indicators→needs, interventions→solution | consume-side depth | No |
| Campaign Phase 6 / 7 | ingest robustness / toolkit hygiene | No |

**The single join point.** Team Phase 5 (team-scoped runbooks become seedable)
inherits R2's soundness precondition: weakly-gated, LLM-authored runbooks must be
trust-gated *before* they seed live — and team's blast radius is larger (a bad
team runbook would seed every teammate, not just its author). The **hard gate**
(what preserves NO-INCORRECT-CONCLUSION) is R1 (validator hardening) + R2
(EXPERIMENTAL filter), both done, + **team Phase 4** (membership-validated writes
*and verify*, in this ADR). The load-bearing reason that set suffices is the
seeder's own engine-side property: a seeded cause is a **CANDIDATE-only prior
with no evidentiary privilege** (`milestone_engine.py`) — subject to the same
decay, anchoring detection, and evidence-required validation as any hypothesis —
so even a bad team runbook can bias attention but *cannot conclude*. Beyond the
hard gate, two campaign units are strong **should-precede** for team Phase 5,
both because team amplifies the flywheel: **R5** (don't let a failed fix's
commands land in a runbook) and **R3** (provenance-uniqueness — team sharing
creates an echo loop where A's seed → B's resolution → B's near-duplicate team
runbook → C's case; R3 is the guard against laundering a seed back in as new
knowledge). **R4** (admin-gated global writes) is a *sibling* of team Phase 4 —
the same KB write-authorization problem for a different scope, sharing the
`/scan` and verify routes — not a prerequisite; done together they complete the
produce-side write-authorization story (team + global) the campaign only
half-covers today. Net: a scheduling constraint that mirrors R1-before-R2,
changing no campaign objective or ordering.

> **Verified-team-runbook trust tier.** A team-verified draft lands at
> `COMMUNITY` — the *same* tier as platform-curated pack runbooks — so one
> teammate's verify click puts an LLM-authored runbook at pack parity for the
> whole team. This ADR accepts that (membership + the candidate-only property
> bound the risk); a team-specific sub-tier is a design point deliberately left
> open (see Open Questions) rather than silently foreclosed.

**Two tracks, one operating model.** Track A (campaign) proceeds on its existing
order — R3 → R4 → R5 → R6 → R7 → R8/R9 → Phase 6 → Phase 7. Track B (team) runs
Phase 1 (repo) → 2 (wiring) → 3 (management API) → 4 (write-validation) as pure
infrastructure whenever there is bandwidth, with team Phase 5 (live seeding)
**hard-gated behind team Phase 4** (and R1/R2, done), **R5 and R3 should-precede**,
and team Phase 6 (Slack) / 7 (cleanup) after. One PR-sized unit per session,
alternating tracks — the campaign never pauses. (Note team Phase 2 onward is also
gated on multi-tenant readiness — see Decision 2 — a dependency outside both
tracks.)

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
   (settled with the identity/RBAC direction) — including whether **verifying** a
   team draft requires a role beyond membership (Phase 4 / Decision 4).
4. **Standalone teams.** Confirmed out of scope here (Cloud-only). Revisit only
   if a self-hosted multi-user org needs local teams.
5. **Multi-tenant readiness (ADR-010 P2).** Team Phases 2–5 depend on
   `TENANT_PROVIDER = multi` becoming bootable (`MULTI_TENANT_READY`, request→org
   wiring, RLS). What does Cloud run in the interim, and is that work sequenced
   ahead of team Phase 2? (Decision 2.)
6. **`team_members` RLS posture.** Add an `organization_id` column + RLS policy to
   `team_members` (a new migration), or rely on the join-through-`teams`
   resolution? The join defends reads for free; the column defends every future
   writer too. (Isolation and RLS.)
7. **Seeding principal.** Does the seeder resolve *all* teams the case owner
   belongs to, or only the case's own `cases.team_id`? These diverge for
   team-originated cases (ADR-012). (Decision 5.)
8. **Verified-team-runbook tier.** Accept `COMMUNITY` (pack parity) for
   team-verified drafts, or introduce a team-specific sub-tier? (Join-point note.)
9. **Slack sync as an isolation writer.** Phase 6 makes an external system a
   writer of `team_members` — the table the read-isolation invariant depends on.
   Its reconciliation model is an isolation concern, not just a consistency one.

---

## References

- [KB Cause Seeder](../knowledge-and-ai/kb-cause-seeder.md) — the flywheel loop
  whose team arm this unblocks; `build_kb_scope_filter` read seam.
- [Document → Runbook Conversion](../knowledge-and-ai/document-to-runbook-conversion.md)
  — produce-side KB scoping.
- [Architectural Design Principles](../core-architecture/architectural-design-principles.md)
  — `TENANT_PROVIDER` seam, tenancy tables.
- [IAM Design](../security/iam-design.md) — organization context and claims.
- `providers/tenancy/factory.py` — `MULTI_TENANT_READY` gate; ADR-010
  forward-consolidation P2 (the multi-tenant readiness this depends on).
- `alembic/versions/…018_rls_tenant_isolation.py` — RLS `_TENANTED_TABLES`
  (`teams` covered, `team_members` not) and the owner/superuser bypass requiring
  the `faultmaven_app` role.

---

**Last Updated**: 2026-07-18 (Proposed — awaiting review)
