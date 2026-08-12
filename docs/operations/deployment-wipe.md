# Deployment Wipe

**Status:** Procedure complete · **Anchor:** fm#819 (Cloud cutover / multi-tenant flip)
**Scope:** the core-repo half. The authoritative cutover runbook is
`docs/operations/cloud-mode-cutover.md` in `faultmaven-enterprise-infra`, which
sequences this procedure alongside the config flip, the identity provisioning and
the Slack agent swap. **This file is the wipe; that file is the cutover.**

---

## What a wipe is for

Returning a deployment to a clean slate — no cases, no evidence, no users, no
tenants — so it can be provisioned fresh. It is the first step of the Cloud
cutover, where the existing data is test data and the multi-tenant flip is
easier against an empty database than against migrated history.

It is **not** a routine operation and it is not reversible. Take a backup first
if the data has any value.

---

## The surfaces

A wipe that covers only the database is not a wipe. Five surfaces hold state,
and they fail differently when missed:

| Surface | Holds | If missed |
|---------|-------|-----------|
| PostgreSQL `faultmaven` | cases, evidence rows, users, orgs, enterprises, `sso_org_mappings` | the obvious one |
| ChromaDB | KB + per-case embeddings | searches return chunks whose SQL rows are gone |
| Object storage (S3/MinIO or `data/evidence/`) | uploaded evidence bytes | orphaned files, and the storage bill |
| Redis | sessions, revocation watermarks, idempotency, SSO state | a stale session or watermark shadows the new tenant |
| Slack agent PVC `credentials.db` | the agent's OAuth refresh credential | a leftover shadows the new credential seed; the agent authenticates as a user that no longer exists |

`fm-wipe-deployment` covers ChromaDB, object storage and Redis, and **verifies
all four of the server-side surfaces**. The database itself is an operator step
(below), and `credentials.db` is an infra step in the cutover runbook.

### ⛔ `faultmaven_slack` is a different database — never drop it

```
faultmaven            ← wipe this one
faultmaven_slack      ← slack_bots, slack_installations, slack_oauth_states
faultmaven_rehearsal  ← disposable; drop at cutover close-out
```

`faultmaven_slack` holds the Slack **workspace installations**. Dropping it
uninstalls the bot from every workspace and forces a manual re-install in each
one — it is the integration's identity, not test data. `fm-wipe-deployment`
refuses outright if its `DATABASE_URL` resolves to it.

---

## The tool

```bash
fm-wipe-deployment                    # inventory: resolve and report, writes nothing
fm-wipe-deployment --verify           # positive verification; exit 5 on residue
fm-wipe-deployment --wipe --confirm-target faultmaven --yes
```

Installed with the package (`[project.scripts]`), so it is on `PATH` in the API
pod as well as in a local checkout.

### Pass the owner DSN

Every mode refuses an RLS-scoped database role. Under `TENANT_PROVIDER=multi`
the application role sees only its own tenant's rows, so an inventory taken
through it under-reports and — the dangerous half — **`--verify` would pass on a
database that still holds another tenant's data**. `kubectl exec` inherits the
pod's `DATABASE_URL`, which is the application role by design, so override it:

```bash
kubectl exec -it deploy/faultmaven-api -- \
  env DATABASE_URL='postgresql+asyncpg://faultmaven:...@host/faultmaven' \
  fm-wipe-deployment
```

### Read the `target:` lines before wiping

The inventory prints a resolved target per surface — database name, ChromaDB URL
or persist directories, bucket and prefix, Redis `host:port/db`. This is the
point of the command. The tooling it replaced (`scripts/purge_case_data.py`,
`scripts/wipe_case_data.py`, both deleted) hardcoded `data/faultmaven.db` and
would print a clean success report against a stale local SQLite file while the
PostgreSQL deployment sat untouched. `--wipe` therefore requires
`--confirm-target` to name the resolved database back, exactly.

### Scale the API down first

For a local `PersistentClient` vector store, wiping a directory the running API
holds open is the hazard `fm-reset-kb` documents: the server can keep serving
reads from deleted files, or recreate a partial tree under the one just removed.
For an external ChromaDB there is no corruption risk, but a wipe with the API up
just gets re-populated by live traffic.

```bash
kubectl -n faultmaven scale deploy/faultmaven-api --replicas=0
```

---

## The database: drop and recreate, never DELETE

**Do not `DELETE` or `TRUNCATE` your way to a clean database.** It does not
produce one, and the ways it fails are quiet:

- **Migration 029 seeds `roles` / `permissions` / `role_permissions` with a bare
  `op.bulk_insert`**, and Alembic will not re-run it on an already-stamped
  database. Delete those rows and the SSO login path's membership write fails its
  `role_id` FK — **every SSO login fails closed**, with nothing to restore the
  seed but a hand-written INSERT.
- **Migration 006 seeds the default `enterprises` row** that the user repository
  still falls back to.
- **`operator_access_grants` and `operator_access_audit` reject DELETE and
  TRUNCATE by trigger** (migration 036), so a blanket truncate aborts part-way
  and leaves the wipe half-applied.

`fm-wipe-deployment --verify` checks for exactly this: the four seeded tables
must be **non-empty**. An empty `roles` is the signature of a DELETE-based wipe.

### PostgreSQL (Cloud)

```sql
DROP DATABASE faultmaven;
CREATE DATABASE faultmaven OWNER faultmaven;
```

**Then re-grant, before migrating.** RLS grants are per-database and
`DROP DATABASE` destroys them. The cluster-level roles (`faultmaven_app`,
`faultmaven_maintenance`) survive; their privileges on that database do not.
Run both provisioning scripts **before** `alembic upgrade head`, so
`ALTER DEFAULT PRIVILEGES` covers the tables the migrations are about to create:

```bash
scripts/apps/provision-rls-app-role.sh        # in faultmaven-enterprise-infra
scripts/apps/provision-maintenance-role.sh
```

Skip this and the app cannot read its own tables, and the `kb_seed` maintenance
job has no role to run under.

Then run the **migration Job** — `RUN_STARTUP_MIGRATIONS` is false on
Kubernetes, so migrations do not come from app startup.

### SQLite (Standalone)

The equivalent, with the API stopped:

```bash
./faultmaven.sh stop
rm data/faultmaven.db
alembic upgrade head
```

---

## Ordered procedure

```
 1. Back up if the data matters. This is irreversible.
 2. fm-wipe-deployment                     # inventory — check every target: line
 3. Scale the API down to 0.
 4. fm-wipe-deployment --wipe --confirm-target faultmaven --yes
 5. DROP DATABASE + CREATE DATABASE        (operator, owner DSN)
 6. provision-rls-app-role.sh + provision-maintenance-role.sh   ← BEFORE migrating
 7. Run the migration Job.
 8. Delete credentials.db from the Slack agent PVC.
 9. fm-wipe-deployment --verify            # ← must pass before provisioning
10. Provision, in this order:
      fm-provision-sso-org --name … --slug … --workos-org-id org_…   (owner DSN)
      human signs in via WorkOS
      fm-promote-platform-admin <username>     (verify the derived username first)
      fm-provision-service-account -u slack-agent -o <organization_id>
      python -m faultmaven.jobs.run kb_seed --cross-tenant-maintenance
11. Scale the API back up; redeploy the Slack agent.
```

Step 9 gates step 10 deliberately: provisioning onto a half-wiped database is
how you get an `enterprise_mismatch` tenant that needs manual migration to fix.

Step 10's order is unforgiving — see `docs/operations/sso-org-provisioning.md`.
An unmapped IdP org fails closed (`sso_org_unmapped`); there is no JIT tenant
creation by design, so the mapping must precede the first sign-in.

---

## Verify positively

**A green pod proves nothing.** Both the admin bootstrap and the KB bootstrap
fail non-fatally, and `/health` is mostly stubs — the API will come up happily
on a half-wiped database and only fail later, at the first SSO login.

`fm-wipe-deployment --verify` is the positive check. It exits **5** unless:

- every data table is empty (33 tables — case domain, identity, tenancy,
  operator-governance, config overrides);
- every migration-seeded table is **non-empty** (`roles`, `permissions`,
  `role_permissions`, `enterprises`);
- the stamped `alembic_version` matches the migrations' head;
- the vector store has no collections;
- object storage has no objects;
- Redis has no keys.

It also exits 5 when a surface could **not be inspected**. Silence from a
surface nobody could read is not a clean bill of health, which is the entire
failure mode this procedure was built around.

`knowledge_items` is reported but never asserted: it is 0 immediately after the
migrations and ~91 after `kb_seed`, and both are correct at their own point in
the sequence. Under `TENANT_PROVIDER=multi` the startup KB bootstrap is skipped,
so the pack comes from the audited `kb_seed` job — not from a restart.

---

## Notes

- **Redis is scoped by default.** `--wipe` deletes keys under FaultMaven's known
  prefixes (`session:`, `revoked:token:`, `idempotency:`, `sso:state:`,
  `sso:login:`) and *reports* the count of keys under any other prefix rather
  than silently skipping them. Use `--redis-all-keys` only if that logical Redis
  database is FaultMaven's alone.
- **FakeRedis has nothing to wipe.** In standalone the keyspace lives inside the
  API process; a CLI process has its own empty copy. The command says so
  instead of reporting "0 keys deleted", which would read as a clean sweep of
  state that is actually in another process. Restart the API to clear it.
- **The service-account org claim is not persisted.** It lives only in the token
  chain and is re-stamped on rotation. Lose the rotated refresh token and you
  must re-run `fm-provision-service-account -o` — see fm#819.
- **`faultmaven_rehearsal`** is the flip-rehearsal namespace's database. Rehearse
  this whole procedure there, including the drop-and-recreate, before the live
  cutover; drop it at close-out.
