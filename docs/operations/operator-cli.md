# Operator console entrypoints (`fm-*`)

Deployment procedures ship *with the installed package*, not as files under
`scripts/` — the wheel excludes `scripts/` and the image never COPYs it, so a
path-based in-pod invocation cannot work (#887). The entrypoints below are
declared in `pyproject.toml` `[project.scripts]`, implemented in
`faultmaven/cli/`, and land on `PATH` wherever FaultMaven is installed (the API
pod; locally after `pip install -e .`).

```bash
# In a pod:
kubectl exec -it deploy/faultmaven-api -- fm-provision-sso-org --name ...
```

Every command acts on the deployment's database, so every command refuses to
run without a persistent one (#1659). An empty `DATABASE_URL`, `:memory:`, an
in-memory SQLite URL or a value that does not parse as a database URL makes it
exit 1 before it reads or writes anything. It prints the API's boot refusal on
stderr and nothing on stdout.

| Command | What it does | Detail |
|---------|--------------|--------|
| `fm-promote-platform-admin <username>` | Promote a user to platform admin (deployment operator) | [Account provisioning](./account-provisioning.md) |
| `fm-demote-platform-admin <username>` | Remove platform admin privileges | [Account provisioning](./account-provisioning.md) |
| `fm-provision-service-account -u slack-agent` | Mint a service-account OAuth refresh credential (`AUTH_MODE=oauth`) | [Service account credentials](./security/service-account-credentials.md) |
| `fm-provision-sso-org --name ... --slug ... --domain acme.com --workos-org-id org_...` | Provision a Cloud tenant (enterprise carrying the customer's domain + organization) and its WorkOS org mapping (`TENANT_PROVIDER=multi`). Creates NO team — teams form by consent (ADR-017 D4) | [SSO org provisioning](./sso-org-provisioning.md) |
| `fm-remove-org-member --enterprise-id ... --organization-id ... --user alice --yes` | Remove a BILLING membership AND revoke that user's tokens, as one operation (#874); the account keeps its enterprise anchor — leaving an organization changes what is metered, not what is visible (ADR-017 D5) | [Account provisioning](./account-provisioning.md) |
| `fm-personal-tenant retire --subject user_01H... --apply` | Retire a JIT personal tenant (fence the enterprise, revoke tokens, stamp the binding retired+policy, delete the WorkOS org by recorded id, delete the mapping); the account STAYS anchored (`users.enterprise_id` is NOT NULL, ADR-017 D3); `--next-login refuse\|fresh-tenant`; dry run by default (#1045 D8) | [SSO org provisioning](./sso-org-provisioning.md) §"Retiring a personal tenant" |
| `fm-personal-tenant re-anchor --subject user_01H... --enterprise-id ... --apply` | Move a personal account onto a mapped company enterprise (billing organization membership is a separate, deliberate act — ADR-017 D5) | [SSO org provisioning](./sso-org-provisioning.md) §"Re-anchoring a personal account to a company enterprise" |
| `fm-personal-tenant purge-idp-org --provider-org-id org_01H... --apply` | Remove a provider-side organization no tenant claims (explicit id only) | [SSO org provisioning](./sso-org-provisioning.md) §"Cleaning up a provider-side organization" |
| `fm-reassign-cases --enterprise-id ... --from-user slack-agent --to-user slack-T0123 --case-ids-file ids.txt --dry-run` | Move cases to a new owner within one ENTERPRISE, with the team share and an audit row; the new owner must be anchored to that enterprise | [Reassigning case ownership](./reassigning-case-ownership.md) |
| `fm-backfill-case-outcome --dry-run` / `--yes` | Compute the case outcome record (`progress.outcome`, #2615) once for every stored case without one, from the case as stored, and repair a terminal case's record no terminal write folded; writes the progress blob alone (version bumped, `updated_at` kept), retries a lost version race, skips a case with work in flight (exit 3: re-run); idempotent; `--enterprise-id` required under `TENANT_PROVIDER=multi` (run once per tenant). A case that fails is listed and the run goes on (exit 1). Run it after the release that writes the record and before any release that switches a reader onto it: no reader computes a missing record | [Backfilling the case outcome record](#backfilling-the-case-outcome-record) |
| `fm-set-turn-cap --enterprise-id ... --organization-id ... --show` | Read one billing subject's daily investigation-turn cap and today's usage (or `--account-id`, read-only) | [SSO org provisioning](./sso-org-provisioning.md) §"Changing one tenant's daily turn cap" |
| `fm-set-turn-cap --enterprise-id ... --organization-id ... --cap 200 --yes` | Raise/lower it, or `--unlimited` / `--clear`; effective on that subject's NEXT turn, no restart (ADR-016 D5.3, re-keyed to a billing subject by ADR-017 D5) | same |
| `fm-reset-kb --dry-run` | Wipe/re-bootstrap the KB (refuses under `TENANT_PROVIDER=multi`) | [Data & storage management](./data-storage-management.md) |
| `fm-wipe-deployment` | Inventory every wipe surface (resolved targets, writes nothing) | [Deployment wipe](./deployment-wipe.md) |
| `fm-wipe-deployment --verify` | Positively verify a clean slate; exit 5 on residue (#819) | same |
| `fm-wipe-deployment --wipe --confirm-target faultmaven --yes` | Wipe vectors + object storage + Redis | same |

## Backfilling the case outcome record

`progress.outcome` (#2615 stage S1) is written at the end of each decision
cycle and at both terminal transitions, so a case stored before that release
has none until its next turn, and a terminal case never takes one. There is no
legacy read path: nothing computes a record when it meets `None`. So
`fm-backfill-case-outcome` runs once per deployment (once per tenant under
`TENANT_PROVIDER=multi`) after the release that writes the record and before
the release that first switches a reader onto it.

- It computes each record from the case as stored, a fold over no prior
  (ruled on #2636). A verdict the case overwrote in place before the record
  existed cannot be recovered.
- It also repairs a terminal case whose record no terminal write folded
  (`folded_under` an open state): what a transition leaves when its record
  write raised. The repair folds over that stored record: its final verdicts
  (a FAILED window, `accepted_in_turn`) are carried, and its closure, the open
  case's, which is not final, is read off the closed case.
- It writes the `progress` blob alone, compare-and-swap on `version`
  (`ICaseRepository.save_progress`). The version is bumped, so a turn that
  loaded the case before the write gets 409 and the user re-sends; `updated_at`
  is kept, so no user's case list re-sorts; no save-time derivation runs (no
  offer lapses). A write that loses the version race is tried again from a
  fresh load: tried up to three times in all.
- It skips a case that needs a record while work on it is in flight: a turn,
  or a task one spawned, holds the case's lease on some replica (the lease a
  case delete reads). A case whose lease cannot be read is skipped too; a case
  that has its record is left alone without reading the lease. Both are listed, the exit status is 3,
  and a re-run picks them up. Under standalone the lease store lives in the
  API's own process, which the command cannot see: run it with the API idle.
- It is idempotent: a case that has a record is left alone.
- A case whose outcome cannot be computed, or whose write fails three times,
  is counted and listed, and the run goes on. The exit status is 1 when any
  case failed.

```bash
fm-backfill-case-outcome --dry-run          # what would be written
fm-backfill-case-outcome --yes              # single-tenant
fm-backfill-case-outcome --enterprise-id <id> --yes   # each tenant under multi
```

Unit tests: `tests/unit/cli/`.
