# WIP — #629 flip rehearsal on staging (Phase B step 1)

Owner queue: `WIP-plan-next-phase.md` Phase B step 1. Claimed 2026-07-28 (fm#629 +
fm#873 assigned). ⛔ The live D10/oauth cutover hard stop stands — everything here
is staging-only; the live `faultmaven` namespace is untouched.

## Ground truth (verified 2026-07-28)

- **#873 prerequisite**: PR #875 open (`feat/873-service-account-org-claim`).
  Two legs: `provision_service_account.py --organization-id` stamps the org
  claim at mint; `OAuthService.refresh_access_token` re-attaches the presented
  token's claim (it previously dropped it — a stamped credential would have
  gone org-less on first rotation). Reviewed + independently verified
  (41 targeted tests, mutation-killed both legs). Awaiting owner merge.
- **There is no staging environment.** The on-prem cluster has ONE namespace
  `faultmaven`, currently in its **interim-production** role
  (infra `docs/operations/environment-lifecycle.md`): 2-replica API serving
  `api.faultmaven.ai`, live slack-agent deployment beside it. The
  `overlays/staging` kustomization exists but is **unapplied** and targets
  `namespace: faultmaven` — it predates the interim-production loan and needs
  rework before it can coexist with the live deployment.
- **The flip set is larger than TENANT_PROVIDER.** The coherence gate
  (`faultmaven/config/deployment_coherence.py`) makes `multi` fatal outside
  `DEPLOYMENT_MODE=cloud`, and cloud fatally requires: `AUTH_MODE=oauth` +
  RS256 keys, PostgreSQL `DATABASE_URL`, Redis sessions, `STORAGE_BACKEND=s3`,
  and **all three `WORKOS_*` non-empty** (ADR-015 hardening). So the staging
  rehearsal set, atomically: `DEPLOYMENT_MODE=cloud`, `AUTH_MODE=oauth`,
  `OAUTH_ENABLED=true`, RS256 pair (bootstrap auto-generates),
  `WORKOS_*` (staging environment values), `TENANT_PROVIDER=multi`, plus the
  data-tier URLs pointed at staging-scoped resources.
- WorkOS **staging is a separate WorkOS environment** — own `client_...`, own
  `sk_...`, own registered redirect URI
  `https://api.staging.faultmaven.ai/api/v1/auth/sso/callback`
  (infra `docs/operations/workos-sso-setup.md` §Staging). WorkOS org ids are
  per-environment: the staging FM org must be provisioned against the staging
  IdP org id, never prod's.

## Environment decision (proposed)

**New namespace `faultmaven-staging` on the same cluster**, with its own
data-tier scope. Rejected alternatives: flipping the live namespace (breaks the
live agent + users — exactly what the hard stop forbids); a docker-compose
rehearsal (misses the two cluster-shaped known gotchas the rehearsal exists to
surface: `kb_seed` vs ChromaDB netpol labels under multi (infra#138), and CD
pipeline behavior — no `--prune`, never applies `kubernetes/platform/`).

Data tier — reuse the existing instances where isolation is logical, dedicate
where it is not:

| Component | Staging approach |
|---|---|
| PostgreSQL | New **database** `faultmaven_staging` in the existing primary; same two-role split (`faultmaven` owner / `faultmaven_app_staging` limited) via `provision-rls-app-role.sh` adapted for the DB name. RLS is per-database — full isolation from prod data. |
| Redis | Separate logical DB index (staging `REDIS_DB=1`) — sessions/caches only, acceptable blast radius for staging. |
| ChromaDB | **Dedicated small instance in `faultmaven-staging`** — collections aren't tenant-scoped across environments, and the netpol-label gotcha needs a staging netpol to reproduce faithfully. |
| MinIO | Same instance, new bucket `evidence-staging` + staging credentials; `S3_KEY_PREFIX` unchanged semantics. |
| Presidio/Opik | Reuse cross-namespace (non-stateful; ClusterIP reachable). |

Overlay rework: `overlays/staging` → `namespace: faultmaven-staging`, service
DNS names updated, internal-only ingress (`api.staging.faultmaven.ai` on the
internal resolver — no public DNS per environment-lifecycle ground state; the
browser doing the SSO round-trip runs inside the network or uses hosts
entries). Migration Job (owner DSN) applied manually — CD does not target
staging.

## Owner inputs required (blocking)

1. **Merge PR #875** (#873) — the staged D10 swap leg needs the script flag.
2. **WorkOS Staging environment**: create (or hand me credentials for) the
   staging environment in dashboard.workos.com; register the staging redirect
   URI; provide `client_...` (goes in the overlay ConfigMap patch, committed)
   and `sk_...` (bootstrap secret, never git). Also create one test
   organization in that environment and note its `org_...` id.
3. **Resource budget confirmation**: the staging API pod (single replica,
   256Mi/250m per the overlay) + small ChromaDB on the box beside
   interim-production. Say if you want tighter caps.

## Rehearsal verification matrix (once built)

1. **Coherence gate negative test first**: boot with one `WORKOS_*` empty →
   pod refuses with the incoherence list; then the full set → clean boot.
2. **RLS bite**: two staging orgs; user A cannot read user B's cases/KB rows
   via API or direct `faultmaven_app_staging` psql session.
3. **SSO login into a mapped org**: `provision_sso_org.py` (owner DSN, staging
   IdP org id) → AuthKit round-trip → JIT user lands in the mapped org with
   member role; unmapped IdP org → `sso_org_unmapped` fail-closed slug.
4. **Global-KB read from a tenant** — expected to surface the `kb_seed`
   netpol-label gotcha (infra#138); capture, fix netpol, re-verify.
5. **Jobs refusal matrix**: maintenance jobs run under audited tenant scopes;
   unscoped job access refused.
6. **Sentinel refusal (#850)**: dev-login absent under oauth; any org-less or
   sentinel-claim token refused at bind.
7. **Staged D10 swap end-to-end**: `provision_service_account.py -u
   slack-agent -o <staging org uuid>` in the staging API pod → refresh via
   `POST /auth/oauth/token` → authenticated case-open API call succeeds under
   multi; rotation preserves the claim (decode and check).
8. **Rollback drill**: staging namespace deleted and rebuilt from the overlay —
   proves the flip is reversible mechanically.

## Sequencing

1. PR #875 merged (owner) — parallel with infra buildout.
2. Infra buildout PR(s) in `faultmaven-enterprise-infra` (overlay rework +
   staging data tier + bootstrap-secrets staging variant).
3. WorkOS staging values land (ConfigMap patch commit + secret bootstrap).
4. Atomic flip on staging namespace → verification matrix → findings filed.
5. Report to #629 with the rehearsal transcript; that unlocks Phase B step 2
   (slack org-binding, #819/slack#25).
