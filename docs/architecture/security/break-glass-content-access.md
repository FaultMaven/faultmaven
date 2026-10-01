# Break-Glass Content Access

How a platform operator reaches a tenant's **case content** — title, description,
transcript — in the Cloud deployment, and why that path is shaped the way it is.

This is the content row of ADR-012 D8/D9. The metadata row (the cross-tenant
case *list*) is `GET /api/v1/admin/cases`; how it spans enterprises under
multi-tenancy is [below](#the-cross-enterprise-list-bounded-by-its-result-type-and-its-grant).
The durable audit trail both paths write to is `operator_access_audit`.

## The boundary

D8/D9 splits what an operator can see into two categories:

| Category | Contents | Standalone | Cloud |
|----------|----------|------------|-------|
| **Metadata** | case id, enterprise, organization, team, state, timestamps, counts | ambient | ambient |
| **Content** | title, description, transcript, evidence | audited, not gated | **break-glass** |

Title is content, not metadata: it is user free-text, so it leaks whatever the
reporter typed.

The deployment split is not a trust ranking. In Standalone the operator and the
data controller are the **same party** — gating an operator against their own
organization's data would be ceremony, so content reads are recorded but not
withheld. In Cloud they are different parties: FaultMaven is a processor acting
on a controller's data, so standing access to content is precisely what the
processor posture forbids.

## The grant

A **grant** is one operator's time-boxed license to read one case's content.

```http
POST /api/v1/admin/grants
{ "case_id": "...", "enterprise_id": "...", "reason": "...", "ttl_minutes": 60 }
→ 201 { "grant_id": "...", "approval_state": "auto_approved", "is_live": true,
        "expires_at": "...", "revoked_at": null, ... }
```

The request is validated before anything is written; a value outside these
limits is a 422 and creates no grant (`BreakGlassGrantRequest` in
`faultmaven/models/api_models.py`):

| Field | Limit |
|-------|-------|
| `reason` | Required. At most 2000 characters as sent (`MAX_GRANT_REASON_LENGTH`); at least 20 once surrounding whitespace is stripped (`MIN_GRANT_REASON_LENGTH`). The stripped value is what is stored. |
| `ttl_minutes` | Optional, default 60 (`DEFAULT_GRANT_TTL_MINUTES`); from 1 to 240 (`MAX_GRANT_TTL_MINUTES`). |
| `case_id`, `enterprise_id` | Required, 1 to 36 characters; longer is rejected, never truncated. |

The grant surface is create (`POST /api/v1/admin/grants`), list
(`GET /api/v1/admin/grants`) and revoke
(`POST /api/v1/admin/grants/{grant_id}/revoke`). There is no extend.

There is no single `state` field, deliberately. Approval, revocation and expiry
are three independent reasons a grant may not authorise anything, so collapsing
them into one word would either lose information a reviewer needs or invite a
client to reconstruct the predicate itself. `is_live` is the server's verdict —
clients render that and never re-derive it from `expires_at`.

Four properties, each of which the security review turns on:

**One case, never a tenant.** A grant names exactly one `case_id`. The operator
already has the case id from the metadata list, so a per-case grant costs nothing
in workflow, and it makes the blast radius of a compromised or over-broad
justification a single case rather than a customer's whole history. An org-scoped
grant would disclose every title and transcript in that tenant on the strength of
one reason string.

**A reason, checked for substance.** `reason` is required, is stored on the grant
*and* denormalised onto every audit row the grant authorises, and is rejected
below `MIN_GRANT_REASON_LENGTH` (20 characters once surrounding whitespace is
stripped). A length floor does not make a justification
meaningful — nothing at this layer can — but it does stop the field degrading
into `"."`, which is the failure mode that makes an audit trail worthless.

**A TTL, and no way to extend one.** The window is 60 minutes unless the request
asks for less or more, and never more than 240. `expires_at` is immutable; the
database rejects an UPDATE that changes it, and the API has no extend route. Needing longer means creating a *new* grant,
with a fresh reason and a fresh audit row. An extendable grant converges on a
standing one, which is the thing this design exists to prevent.

**Auto-approved today, with the approval seam already in the schema.** The grant
carries `approval_state`, `approved_by` and `approved_at`. Today an operator's
own grant is created `auto_approved`: the control is reason + TTL + an immutable
trail, which is what a SOC 2 / ISO 27001 reviewer asks to see. Customer-initiated
approval — the stronger posture ADR-012 D9 calls the ideal — is a transition in
that state machine (`pending → approved`) plus a tenant-admin surface to drive
it, not a schema change or a re-shaping of the read path. It is deliberately not
built now, because the tenant-admin approval and notification surface it needs
does not exist and is a workstream of its own.

Liveness is one predicate, and only one place computes it:

```sql
approval_state IN ('auto_approved', 'approved')
  AND revoked_at IS NULL
  AND expires_at > now()
```

## Reaching the content

```http
GET /api/v1/admin/cases/{case_id}            → case detail (title, description, state)
GET /api/v1/admin/cases/{case_id}/messages   → transcript
```

Both are operator-only, both resolve the same gate, and both answer with an
envelope that says how they were reached:

```json
{ "access": "break_glass", "grant": { "grant_id": "...", "expires_at": "..." }, "case": { ... } }
{ "access": "standing",    "grant": null,                                       "case": { ... } }
```

The envelope is a discriminated union for the same reason `GET /admin/cases`'s
is: the UI renders what the backend actually served rather than what it infers
from its own notion of the deployment, so the two cannot drift and a mode misread
cannot present break-glass content as ordinary access.

Every read records `OperatorAction.CONTENT_OPEN` **before** the content is
served, carrying the grant id, the reason and the expiry. A failure to record is
a 503, not a warning — the same fail-closed rule as the list path, and for the
same reason: "served but unaudited" silently removes the control.

### Why this is a separate endpoint

`GET /api/v1/cases/{case_id}` gates on owner ∪ shared-to-my-teams with no
operator arm, and it is the single-case gate that transitively guards reports,
exports, analytics and messages. Adding an operator bypass *there* would widen
every one of those paths at once, on a check that runs for every ordinary user
request. The break-glass surface is instead its own route, so the elevated path
is the one that carries the elevated machinery and the ordinary path is
unchanged.

This is also what makes the Standalone operator's All Cases view openable
(faultmaven#846): its rows link here, not into the user case route that 404s them.

## Multi-tenancy: rebind, do not bypass

Under `TENANT_PROVIDER=multi` the target case belongs to another enterprise, so
PostgreSQL RLS hides it from the operator's session. The obvious fixes are all
bad: a `BYPASSRLS` engine in the web process is bounded only by call-site
discipline and can read every tenant's transcripts, and the offline maintenance
role is a jobs runner, not a request path.

Neither is necessary. RLS scopes each transaction from
`app.current_enterprise_id`, which the engine's `begin` listener reads from a
contextvar that `bind_request_enterprise_context` sets per request. So the
elevated read does not need to escape the policy — it needs to be **bound
somewhere else**:

```text
grant validated → set_current_enterprise_id(grant.target_enterprise_id) → read
```

The session stays RLS-enforcing throughout. It never sees more than one
enterprise; it sees a *different* one, named by a grant row, for the duration
of one handler that performs one read. The bound is structural rather than
procedural, which is the property the `BYPASSRLS` options lack.

The rebind is applied **only** under `multi`. Under `single` every row carries
the Standalone enterprise, so rebinding to anything else would make the read
return nothing.

### The grant is not validated against the case

Creating a grant does not check that the case exists or that it belongs to the
named enterprise. This is deliberate, and it is the more secure choice:

- Under `multi` such a check **cannot** work — RLS hides the very row it would
  read — so validating would behave differently per tenancy, which is exactly the
  drift this design avoids elsewhere.
- A validating endpoint is an existence oracle. An operator could probe whether a
  case id exists in a tenant they hold no grant for, which is metadata disclosure
  through the grant API.
- A wrong `(case_id, enterprise_id)` pair **fails closed on its own**: rebinding
  to the named enterprise and reading the named case returns nothing, and the
  operator gets a 404. The mistake costs an audit row and a failed request, and
  discloses nothing.

The corollary matters as much as the rule: because the grant's enterprise is
an operator's unverified assertion, it is **not** what the audit trail records.
The trail is stamped with the enterprise the read actually ran under. Under
`multi` those coincide — the rebind has already made the claim load-bearing, so
a false one returns no rows — but under `single` nothing exercises the claim, and
recording it would let the audited party choose which tenant their own immutable
row names. Attribution comes from the request, never from the assertion.

## The cross-enterprise list: bounded by its result type and its grant

The metadata **list** (`GET /api/v1/admin/cases`) is the one operator read that
rebinding cannot serve under `multi`: it must span every enterprise at once, and
there is no single enterprise to bind to. An ordinary case query from the web
process would return the bound enterprise's cases only — a list that claims to
cover every tenant and shows one.

It reads through two `SECURITY DEFINER` functions instead, created by migration
`003_admin_case_metadata`: `admin_case_metadata_page(state, source, limit,
offset)` returns one page, newest update first, and
`admin_case_metadata_count(state, source)` returns the number of matches in
every enterprise. A migration creates them, so they are owned by the migrating
role — the table owner. The baseline's policies are `ENABLE`d and never
`FORCE`d, and PostgreSQL exempts a table's owner from a non-forced policy, so the
functions span every enterprise while the session that calls them stays
RLS-scoped for everything else it does. They are `LANGUAGE sql`, `SECURITY
DEFINER`, `SET search_path = pg_catalog, public, pg_temp` — `pg_temp` listed, and
last, because a definer function that leaves it out searches the caller's
temporary schema *first* for relations, and a caller's temporary `cases` would
shadow the real table. They also pin `row_security = off`: the functions span
every enterprise only while their owner is exempt from the policies, and if that
ever stops being true (`FORCE ROW LEVEL SECURITY` on `cases` or
`resource_shares`, or a migrating role that does not own them) a read the
policies would filter raises instead of quietly returning the caller's own
enterprise as though it were all of them.

A page is chosen on narrow columns (`case_id`, `updated_at` and the filter
columns), newest update first with `case_id` breaking ties — the same order the
single-tenant repository lists in, so a page boundary between cases a single
statement updated falls in the same place on both paths — and only the page's
rows are joined back for their JSON. `limit` and `offset` are `bigint`, because
the API bounds neither to 32 bits.

Two things bound the bypass, and it needs both.

**The result type.** The page function returns system ids, timestamps,
integers, booleans, arrays of those, and three short strings — nothing sourced
from `title`, `description` or any other user text, and no JSON blob. There is no
argument or caller that makes it return content, because it has no column to
return it in. Of the three strings, `state` is a closed vocabulary the database
enforces (`cases_state_check`). `source` and `closure_reason` are closed
vocabularies too, but the application's writers enforce them — the `Case` model's
`Literal` for `source` and its closure-reason validator — not the column type,
which carries no CHECK. `tests/integration/security/test_admin_case_metadata_postgres.py`
asserts the declared result columns against the catalog, so adding one fails
until a person classifies it.

**An explicit grant.** A new PostgreSQL function is executable by `PUBLIC`, and
every login role holds `CONNECT` on the database through `PUBLIC`, so left at the
default any role able to connect to the cluster could read every enterprise's
case metadata. Revision `003` revokes `EXECUTE` from `PUBLIC` on both functions
and grants it to the runtime role `faultmaven_app` — only if that role exists
when the migration runs, so a database without it still migrates. A deployment
whose runtime role has a different name, or that creates the role after this
revision ran, must grant `EXECUTE` on both functions to it itself; until it does,
the list answers 503 and says which grant is missing.

**Two fields are derived, not stored.** The row's `stage` comes from four gate
milestones inside the `progress` JSON, and its `investigation_turn` from the
out-of-band entries of `metadata.turn_history`. The function returns only their
primitive inputs — the four booleans, and every history entry's turn number
with whether it was an aside, in stored order — and the application applies the
same rules a loaded case applies (`CaseMetadata.from_stored`, over the module
functions the `Case` properties delegate to), including the turn-sequence repair
every case load performs. Neither rule exists in SQL. The single-tenant list and
this one are two paths to the same `AdminCaseMetadata` row, kept in step by a
parity test on PostgreSQL that serves one fixture set through both and compares
every field.

**A malformed case is listed, not fatal and not dropped.** Every cast out of the
JSON is guarded. A key that is *missing* reads as the model's default (a gate the
blob does not record is `false`), while a value of the *wrong type* — a gate
that is not a boolean, a turn entry that is not an object or has no integral
`turn_number` — reads as `NULL`, and the case is served with its columns and
with the derived fields it cannot compute left null, plus a warning naming the
case and its enterprise. The operator list is how a broken case gets found, so
one such row never fails the page. This is the one place the two paths differ:
the single-tenant list cannot load such a case at all. (A case whose owner
account was deleted is still left out on both paths, as before.)

**Failure direction.** The reader is composed only under `multi`. If it is
missing, if the database has not been migrated to `003`, if the runtime role
lacks `EXECUTE` on the functions, or if the database refuses the read for any
other reason, the route answers 503 — each with its own log event and detail. A
refusal (SQLSTATE `42501`) is not read as "EXECUTE is missing" on its own: a
read row-level security would filter raises the same code under
`row_security = off`, so the reader asks the database
(`has_function_privilege`) which of the two it was. It never falls back to the
RLS-narrowed case query.

The rejected alternatives are the ones rejected for content above — a
`BYPASSRLS` engine in the web process, the maintenance role in a request path —
plus rebinding once per enterprise, which would make a grant-authorised
mechanism ambient.

### The account list needs none of this

The operator's account list (`GET /api/v1/admin/users`) also spans every
enterprise under `multi`, and it carries user free-text — each account's email
address and display name. Account records — who holds an account, in which
enterprise, of which kind, whether it is active — are the service's own
operational data about its users; case content is held on a customer's behalf,
which is why it stays behind the grant this document describes.

It needs no definer function. `users` is outside row-level security — the login
path reads it before any tenant is bound — so an ordinary query from the
runtime role already spans every enterprise. What bounds the read is what it
selects (eleven columns: `user_id`, `enterprise_id`, `email`, `display_name`,
`account_kind`, `service_channel`, `is_active`, `is_email_verified` and three
timestamps — never the password hash, the SSO subject, a token, a preference or
the role list) and where it is called from (that route alone, under `multi`,
behind `platform_admin`). Each read is recorded before anything is served
(`action: list`, `details.surface: "accounts"`, `target_enterprise_id` set to
the `enterprise_id` filter, NULL when the read spans every enterprise; the case
list records `surface: "cases"`), and a read that cannot be recorded is
refused. The search text is not recorded — only `search_present` — because it
is often an email address and the trail is append-only: nothing written there
can be erased. Roles are reported only for accounts in the operator's own
enterprise, and those are the only rows marked `manageable`; administering an
account stays confined (below). `docs/architecture/security/rbac.md` → "User
Administration" has the rest.

### What is still deferred

Evidence **file** content is likewise not yet reachable through this path. The
grant model covers it unchanged — same gate, same audit action — but the file
download surface carries its own storage and redaction concerns and is tracked
separately.

**User administration is deliberately outside this model, not pending inside
it.** Listing accounts is metadata and spans enterprises (above); administering
one is not. The routes that read one account or change one
(`/api/v1/admin/users/{id}*` and the `/api/v1/auth/users*` operator routes,
including that older listing) are confined to the operator's own enterprise by a
tenant predicate and have no cross-tenant path at all (#1318,
`docs/architecture/security/rbac.md` → "User Administration"). A grant cannot
serve them as it stands: `target_case_id` is `NOT NULL`, the lookup keys on it,
and the rebind derives the RLS scope from the request's own `target_enterprise_id`
— so reaching a *user* would mean minting a grant whose stated justification
names an unrelated case, and writing that into an append-only trail. Making the
case id optional would be a second grant model wearing this one's schema.
Extending break-glass to user administration is a design change with its own
target type, and is tracked on #1318 rather than approximated here.

## Immutability

The audit trail (`operator_access_audit`) rejects UPDATE, DELETE and TRUNCATE at
the database. The `TRUNCATE` guard is a statement trigger, because row triggers
do not fire on `TRUNCATE`; without it the append-only claim would hold only
"given the current GRANTs" rather than absolutely.

The grant table is not append-only — revocation and approval are real UPDATEs —
but the rule its triggers enforce is that **access can only ever be narrowed in
place**. Widening it takes a new row, with a fresh justification:

| Mutation | Permitted? |
|----------|------------|
| Any column but the approval/revocation pairs | **No** — pinned outright |
| First revocation (`revoked_at` NULL → set) | Yes — narrowing |
| Clearing or moving `revoked_at` | **No** — revocation is monotonic |
| `pending → approved` | Yes — what the approval seam exists to do |
| `denied → approved` | **No** — a denial is final |
| DELETE, TRUNCATE | **No** |

An operator can end their own access early; they cannot rewrite why they took
it, extend how long they were allowed to keep it, or reverse a customer's
refusal.

The audit row denormalises `reason` and `expires_at` rather than only referencing
`grant_id`, so the evidence of an access is complete even if the grant row is
ever lost.

## Rejecting over-long identifiers

Identifiers that arrive from the request path are validated and **rejected**
rather than truncated to their column bound. A >36-character case id, silently
clipped, would produce an immutable audit row naming a *different, real* case —
an access recorded against a case that was never opened. Values derived from a
verified JWT are still clipped: they cannot be attacker-shaped into a collision,
and failing the request would take down an audited read over a long username.
