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

| Command | What it does | Detail |
|---------|--------------|--------|
| `fm-promote-platform-admin <username>` | Promote a user to platform admin (deployment operator) | [Account provisioning](./account-provisioning.md) |
| `fm-demote-platform-admin <username>` | Remove platform admin privileges | [Account provisioning](./account-provisioning.md) |
| `fm-provision-service-account -u slack-agent` | Mint a service-account OAuth refresh credential (`AUTH_MODE=oauth`) | [Service account credentials](./security/service-account-credentials.md) |
| `fm-provision-sso-org --name ... --slug ... --domain acme.com --workos-org-id org_...` | Provision a Cloud tenant (enterprise carrying the customer's domain + organization) and its WorkOS org mapping (`TENANT_PROVIDER=multi`). Creates NO team — teams form by consent (ADR-017 D4) | [SSO org provisioning](./sso-org-provisioning.md) |
| `fm-remove-org-member --enterprise-id ... --organization-id ... --user alice --yes` | Remove a BILLING membership AND revoke that user's tokens, as one operation (#874); the account keeps its enterprise anchor — leaving an organization changes what is metered, not what is visible (ADR-017 D5) | [Account provisioning](./account-provisioning.md) |
| `fm-personal-tenant retire --subject user_01H... --apply` | Retire a JIT personal tenant (fence the enterprise, revoke tokens, stamp the binding retired+policy, delete the WorkOS org by recorded id, delete the mapping); the account STAYS anchored (`users.enterprise_id` is NOT NULL, ADR-017 D3); `--next-login refuse\|fresh-tenant`; dry run by default (#1045 D8) | [SSO org provisioning](./sso-org-provisioning.md) §Personal tenants |
| `fm-personal-tenant re-anchor --subject user_01H... --enterprise-id ... --apply` | Move a personal account onto a mapped company enterprise (billing organization membership is a separate, deliberate act — ADR-017 D5) | same |
| `fm-personal-tenant purge-idp-org --provider-org-id org_01H... --apply` | Remove a provider-side organization no tenant claims (explicit id only) | same |
| `fm-reassign-cases --enterprise-id ... --from-user slack-agent --to-user slack-T0123 --case-ids-file ids.txt --dry-run` | Move cases to a new owner within one ENTERPRISE, with the team share and an audit row; the new owner must be anchored to that enterprise | [Reassigning case ownership](./reassigning-case-ownership.md) |
| `fm-set-turn-cap --enterprise-id ... --organization-id ... --show` | Read one billing subject's daily investigation-turn cap and today's usage (or `--account-id`, read-only) | [SSO org provisioning](./sso-org-provisioning.md) §The daily turn cap |
| `fm-set-turn-cap --enterprise-id ... --organization-id ... --cap 200 --yes` | Raise/lower it, or `--unlimited` / `--clear`; effective on that subject's NEXT turn, no restart (ADR-016 D5.3, re-keyed to a billing subject by ADR-017 D5) | same |
| `fm-reset-kb --dry-run` | Wipe/re-bootstrap the KB (refuses under `TENANT_PROVIDER=multi`) | [Data & storage management](./data-storage-management.md) |
| `fm-wipe-deployment` | Inventory every wipe surface (resolved targets, writes nothing) | [Deployment wipe](./deployment-wipe.md) |
| `fm-wipe-deployment --verify` | Positively verify a clean slate; exit 5 on residue (#819) | same |
| `fm-wipe-deployment --wipe --confirm-target faultmaven --yes` | Wipe vectors + object storage + Redis | same |

Unit tests: `tests/unit/cli/`.
