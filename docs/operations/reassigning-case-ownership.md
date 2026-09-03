# Reassigning case ownership

`fm-reassign-cases` moves a set of cases from one owner to another **inside one
organization**. It exists for the Slack per-workspace cutover
(faultmaven-slack-agent#61 step 4): the global `slack-agent` account owns every
Slack case opened before workspaces were bound to their own service accounts,
and those cases must be re-owned before that account is retired.

## Why not just delete the old account

`cases.user_id` is `ON DELETE SET NULL` — one of 19 such FKs on `users.user_id`.
Deleting an owning account neither cascades nor blocks: it **nulls the owner**,
dropping the cases out of every owner-scoped view, unrecoverably. Reassign
first, then retire the account — and retire it by **deactivating** it, not by
deleting the row (see "After the move").

## What it writes

All in one transaction, so a failure part-way leaves the deployment as it was:

| table | change |
|---|---|
| `cases` | `user_id` → the new owner, `version + 1`, `updated_at` |
| `resource_shares` | one `case`→`team` row per case per Team the **new** owner is in |
| `resource_shares` | **deletes** rows for teams the **old** owner was in and the new one is not |
| `user_audit_log` | one `case_reassigned` row per case |

It does **not** touch `cases.organization_id`, `cases.last_activity_at`,
`case_messages.author_id`, or `uploaded_files.uploaded_by`. The last two are
attribution: migration 037 makes `author_id` deliberately un-foreign-keyed so
that "attribution must outlive the account it describes", and ADR-011 D5 calls
the record un-backfillable. Ownership is current state and moves; authorship is
past fact and does not.

## Running it

The case ids come from the agent's own thread→case map — the **only** place a
case's Slack workspace is recorded, since the backend has no such column:

```bash
kubectl exec -n faultmaven <slack-agent-pod> -- python -c \
  "import sqlite3;print('\n'.join(r[0] for r in sqlite3.connect(
   'file:/app/data/cases.db?mode=ro',uri=True).execute(
   \"SELECT case_id FROM thread_cases WHERE team_id='T0B9XNZDR44'\")))" > ids.txt

kubectl exec -i -n faultmaven deploy/faultmaven-api -- fm-reassign-cases \
  --organization-id <org-id> \
  --from-user slack-agent --to-user slack-T0B9XNZDR44 \
  --case-ids-file /dev/stdin --dry-run < ids.txt
```

Re-run with `--yes` to write. `--organization-id` is an **id, not a slug**: it
binds the RLS scope, so the command runs under the pod's own `faultmaven_app`
role rather than needing the RLS-exempt owner DSN.

## What it refuses, and why

* **A named case the source does not own** — a typo, the wrong organization, or
  an already-completed run. Hard refusal; no flag.
* **The source owns cases the file does not name** — under a shared account that
  is what a *second* Slack workspace's history looks like, and merging it would
  put two customers' investigations in one account. Refused unless you pass
  `--leave-unnamed`, having confirmed those cases belong elsewhere.
* **A target that is not an active member of `--organization-id`** — `users` is
  not tenant-scoped, so a mistyped `--to-user` resolves to a real account
  somewhere. The **source** is deliberately not held to this: the global
  `slack-agent` holds no membership row anywhere, which is what it is being
  retired for.
* **A target in no Team** — the moved cases would stay owner-only, invisible to
  every human, while cases created after the bind are team-visible. That is what
  an unfinished bind looks like. `--allow-no-team` proceeds deliberately.

## Stop the agent first

The `version` bump is what stops an in-flight turn silently restoring the old
owner: the versioned save writes `user_id` back from its in-memory `Case`, so
without the bump it would pass the OCC check. But `POST /cases/{id}/turns`
**deliberately does not retry** on an OCC conflict — LLM turns are expensive and
non-idempotent — so it returns 409 and the turn is **lost**. The Slack agent
surfaces that as "the case is busy" and the user re-sends. Run this with the
agent stopped, or in a quiet window.

## Ordering, for the Slack cutover

The case gate is `owned ∪ shared-to-my-teams`. Between the workspace bind and
this command, a turn on a legacy thread authenticates as the new account, is
refused, and 404s — and the agent evicts the thread→case mapping on exactly that
error, destroying the only record of which case belonged to which thread.

**Back up `/app/data/cases.db` before the bind, and run this immediately after.**

## After the move

Retire the old account by setting `is_active=false`. Do **not** delete the row:
`case_messages.author_id` has no FK by design and would be left dangling with no
`display_name` to resolve it, and the surviving `uploaded_files.uploaded_by`
values would be nulled by their `ON DELETE SET NULL`.

## Exit codes

| 0 | success, or a dry run |
| 1 | refused, or rolled back — **nothing was written** |
| 2 | argparse usage error |

There is no half-state code: every write is in one transaction, so an
interrupted run is re-run, not finished.
