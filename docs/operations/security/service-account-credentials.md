# Service Account Credentials

How a non-human actor — today the Slack agent — authenticates to the FaultMaven
API. Implements ADR-012 D10.

## Why this exists

At `AUTH_MODE=local`, the Slack agent authenticates with passwordless dev-login
(`POST /api/v1/auth/dev-login`, username `slack-agent`). At `AUTH_MODE=oauth`
that endpoint is not mounted and returns 404, so the agent needs a credential of
its own.

FaultMaven does not have a client-credentials/M2M grant, and does not need one:
the existing `refresh_token` grant is already headless. An operator mints one
initial refresh token; the agent exchanges it for access tokens and receives a
rotated refresh token each time. No human interaction after the bootstrap.

The human sign-in path (WorkOS AuthKit, ADR-015) is unrelated — WorkOS is never
in the service account's path. FaultMaven mints and owns these tokens.

## Minting a credential

`fm-provision-service-account` is a console entrypoint shipped with the
installed package (`faultmaven/cli/provision_service_account.py`), so it is on
`PATH` in the API pod and in any environment where `pip install -e .` has been
run.

Run it in the API environment (it reads the same settings and signing keys the
API verifies tokens with):

```bash
fm-provision-service-account --username slack-agent
```

In Kubernetes:

```bash
kubectl exec -it deploy/faultmaven-api -- \
    fm-provision-service-account -u slack-agent --token-only
```

The script:

1. Creates the account if it is missing, with `account_kind='slack'` (ADR-012),
   or reuses the existing account — keeping its `user_id`, so historical Slack
   cases stay attached to it.
2. Corrects `account_kind` if the account exists with the wrong one.
3. Mints an initial refresh token and prints it **once**.

`--token-only` puts the token alone on stdout and all progress on stderr, so it
can be redirected straight into a secret without touching the terminal.

The token is not recoverable afterwards. Nothing stores it server-side —
refresh tokens are stateless JWTs; only *revocations* are stored (in Redis).

Requires `AUTH_MODE=oauth`. In local mode the script refuses and tells you to
keep using dev-login.

## Multi-tenant: the credential names its tenant

Under `TENANT_PROVIDER=multi` (Cloud), `--organization-id` / `-o` is **required**:

```bash
fm-provision-service-account \
    -u slack-agent -o 22222222-2222-2222-2222-222222222222
```

The `users` table has no organization column — affiliation is a row in
`organization_members`, and RLS is the authority on it. A credential's tenancy
therefore travels in its own token chain (see
`docs/architecture/security/sso-org-mapping.md`), and the mint is where that
chain starts: the script stamps the organization on the account before signing,
so the refresh token carries an `organization_id` claim.

The organization id is the FaultMaven organization UUID —
`fm-provision-sso-org` reports it when it provisions the tenant, and
it is what your operator records should hold. It is *not* the IdP's `org_01H…`
identifier.

The script refuses, before touching the account, in three cases:

| Refusal | Why |
|---------|-----|
| Multi-tenant with no `--organization-id` | An org-less credential resolves to an **empty** organization claim, and every request it makes is then refused at `bind_request_org_context`. The credential would be dead on arrival, and the failure would only surface as the agent's first API call being rejected. |
| `--organization-id` set to the Standalone sentinel (`00000000-0000-0000-0000-000000000001`) | The sentinel identifies the single-tenant *deployment*, not a tenant — migration 033 keys the global-KB write policy on it. It is refused at mint as well as at bind. |
| Single-tenant with `--organization-id` | A single-tenant deployment has exactly one tenant, so the flag cannot be honoured. Omit it. |

The claim then rides rotation. Both refresh paths — `POST /auth/refresh` and the
oauth refresh grant the agent uses (`POST /auth/oauth/token`,
`grant_type=refresh_token`) — re-attach the presented token's organization claim
to the reloaded user before minting the next pair, so the tenant survives an
unbounded number of rotations without the operator touching it again.

If the agent starts getting 403s at bind time after an otherwise healthy
provisioning run, decode its refresh token (`organization_id` claim) — an empty
claim means the credential was minted org-less and must be re-provisioned with
`-o`.

Standalone / single-tenant deployments ignore all of this: the claim is the
Standalone sentinel, which there is the correct answer.

## How the credential renews

- The agent calls `POST /api/v1/auth/oauth/token` with
  `grant_type=refresh_token` and receives a new access token **and a new refresh
  token**.
- **Rotation is unconditional.** The presented refresh token is single-use: it
  is revoked as part of the exchange. The agent must persist the returned token
  *before* relying on it (write-before-use).
- The window **slides**. Each refresh mints a fresh token with a full
  `JWT_REFRESH_TOKEN_EXPIRY_DAYS` (default 7 days) lifetime. There is no absolute
  cap, so a continuously running agent never needs re-provisioning.

## Lockout modes and recovery

Recovery from every mode below is the same: **re-run the provisioning script**
and give the agent the new token. Re-running does not revoke previously issued
credentials, so it is safe to run against a still-healthy agent.

| Mode | What happens |
|------|--------------|
| Crash between refreshing and persisting | The presented token was already revoked server-side; the unpersisted new one is lost. The agent restarts with a dead credential. |
| Concurrent refresh | Two refreshes with the same token: the first rotates it, the second presents a revoked token and fails. The agent single-flights refreshes to prevent this; running more than one replica against one credential reintroduces it. |
| Outage longer than the refresh window | The stored refresh token expired. |
| Account deactivated | A refresh reloads the user and rejects inactive accounts. Reactivate the account, then re-run. |

The agent reports an expired or revoked credential as a re-bootstrap error
rather than retrying, so this shows up in its logs as a clear operator action.

## Revoking access

Deactivate the account. Both refresh paths reload the user and reject an
inactive one, so the credential stops renewing and the last access token expires
within `JWT_ACCESS_TOKEN_EXPIRY_MINUTES` (default 15 minutes).

To cut off outstanding access tokens immediately rather than waiting out that
window, also call `POST /api/v1/auth/users/{user_id}/revoke-tokens` (admin).
It writes a per-user revocation watermark, which invalidates every token the
account already holds (#769). Deactivation remains the durable control — it
stops the credential renewing — while the watermark closes the access-token
expiry window.

Two operational limits to know before relying on it:

- **Revocation state lives only in Redis.** Standalone runs FakeRedis
  in-process, so every watermark and revoked jti is lost on API restart, and
  revoked-but-unexpired tokens become usable again for the remainder of their
  lifetime. Deactivation (which is in the database) survives a restart;
  revocation does not. Treat a restart during incident response as re-opening
  the window, and deactivate as well as revoke.
- **Upgrading across the #769 key-namespace change orphans older entries.**
  Per-token keys moved from `{prefix}{jti}` to `{prefix}jti:{jti}`, so
  revocations recorded by an earlier build are no longer read. Access tokens
  age out within `JWT_ACCESS_TOKEN_EXPIRY_MINUTES`, but a refresh token revoked by
  rotation or logout would come back for its full lifetime. Flush the
  `{prefix}*` keyspace (default prefix `revoked:token:`) as part of that
  upgrade if any previously revoked credential must stay dead.

## Rolling back to dev-login

Only possible while the backend is in `local` mode — `oauth` mode does not serve
dev-login at all. Clear the agent's `FAULTMAVEN_REFRESH_TOKEN` and set
`FAULTMAVEN_DEV_LOGIN_USERNAME=slack-agent`. If the backend has already cut over
to `oauth`, rolling the agent back means rolling the backend's `AUTH_MODE` back
with it.

## Blast radius

A failure of this credential degrades **Slack only**. The dashboard and Copilot
authenticate through WorkOS/PKCE and are unaffected.
