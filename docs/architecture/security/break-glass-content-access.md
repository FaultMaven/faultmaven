# Break-Glass Content Access

How a platform operator reaches a tenant's **case content** — title, description,
transcript — in the Cloud deployment, and why that path is shaped the way it is.

This is the content row of ADR-012 D8/D9. The metadata row (the cross-tenant
case *list*) is documented alongside `GET /api/v1/admin/cases`; the durable audit
trail both paths write to is `operator_access_audit` (migration 035).

## The boundary

D8/D9 splits what an operator can see into two categories:

| Category | Contents | Standalone | Cloud |
|----------|----------|------------|-------|
| **Metadata** | case id, organization, team, state, timestamps, counts | ambient | ambient |
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
{ "case_id": "...", "organization_id": "...", "reason": "...", "ttl_minutes": 60 }
→ 201 { "grant_id": "...", "approval_state": "auto_approved", "is_live": true,
        "expires_at": "...", "revoked_at": null, ... }
```

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
below `MIN_REASON_LENGTH`. A length floor does not make a justification
meaningful — nothing at this layer can — but it does stop the field degrading
into `"."`, which is the failure mode that makes an audit trail worthless.

**A TTL, and no way to extend one.** `expires_at` is immutable; the database
rejects an UPDATE that changes it. Needing longer means creating a *new* grant,
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

Under `TENANT_PROVIDER=multi` the target case belongs to another organization, so
PostgreSQL RLS hides it from the operator's session. The obvious fixes are all
bad: a `BYPASSRLS` engine in the web process is bounded only by call-site
discipline and can read every tenant's transcripts, and the offline maintenance
role is a jobs runner, not a request path.

Neither is necessary. RLS scopes each transaction from
`app.current_org_id`, which the engine's `begin` listener reads from a contextvar
that `bind_request_org_context` sets per request. So the elevated read does not
need to escape the policy — it needs to be **bound somewhere else**:

```text
grant validated → set_current_org_id(grant.target_organization_id) → read
```

The session stays RLS-enforcing throughout. It never sees more than one
organization; it sees a *different* one, named by a grant row, for the duration
of one handler that performs one read. The bound is structural rather than
procedural, which is the property the `BYPASSRLS` options lack.

The rebind is applied **only** under `multi`. Under `single` every row carries
the Standalone org, so rebinding to anything else would make the read return
nothing.

### The grant is not validated against the case

Creating a grant does not check that the case exists or that it belongs to the
named organization. This is deliberate, and it is the more secure choice:

- Under `multi` such a check **cannot** work — RLS hides the very row it would
  read — so validating would behave differently per tenancy, which is exactly the
  drift this design avoids elsewhere.
- A validating endpoint is an existence oracle. An operator could probe whether a
  case id exists in a tenant they hold no grant for, which is metadata disclosure
  through the grant API.
- A wrong `(case_id, organization_id)` pair **fails closed on its own**: rebinding
  to the named org and reading the named case returns nothing, and the operator
  gets a 404. The mistake costs an audit row and a failed request, and discloses
  nothing.

The corollary matters as much as the rule: because the grant's organization is
an operator's unverified assertion, it is **not** what the audit trail records.
The trail is stamped with the organization the read actually ran under. Under
`multi` those coincide — the rebind has already made the claim load-bearing, so
a false one returns no rows — but under `single` nothing exercises the claim, and
recording it would let the audited party choose which tenant their own immutable
row names. Attribution comes from the request, never from the assertion.

### What is still deferred

The all-tenant metadata **list** still refuses (403) under `multi`. It cannot be
solved by rebinding — it must span every organization at once, and there is no
single org to bind to. The settled answer is a `SECURITY DEFINER` function owned
by the `faultmaven` role returning metadata columns only, so the bypass is
bounded by a *return type that physically cannot carry a title or description*.
That is not built yet: `multi` cannot boot today (it is gated behind the WorkOS
organization→FaultMaven organization SSO mapping), and shipping PostgreSQL-only
SQL that no deployment exercises would be untested by construction. It lands with
the `multi` cutover.

Evidence **file** content is likewise not yet reachable through this path. The
grant model covers it unchanged — same gate, same audit action — but the file
download surface carries its own storage and redaction concerns and is tracked
separately.

## Immutability

The audit trail (`operator_access_audit`) rejects UPDATE, DELETE and TRUNCATE at
the database. The `TRUNCATE` guard is a statement trigger, because row triggers
do not fire on `TRUNCATE`; without it the append-only claim would hold only
"given the current GRANTs" rather than absolutely.

The grant table is not append-only — revocation is a real UPDATE — but the
columns that constitute the justification are:
`operator_user_id`, `target_case_id`, `target_organization_id`, `reason`,
`created_at` and `expires_at` are pinned by a trigger, and DELETE is rejected
outright. Revocation and approval are the only permitted mutations. An operator
can end their own access early; they cannot rewrite why they took it, or for how
long they were allowed to.

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
