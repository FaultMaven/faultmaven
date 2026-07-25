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

Run in the API environment (it reads the same settings and signing keys the API
verifies tokens with):

```bash
python scripts/auth/provision_service_account.py --username slack-agent
```

In Kubernetes:

```bash
kubectl exec -it deploy/faultmaven-api -- \
    python scripts/auth/provision_service_account.py -u slack-agent --token-only
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

## How the credential renews

- The agent calls `POST /api/v1/auth/oauth/token` with
  `grant_type=refresh_token` and receives a new access token **and a new refresh
  token**.
- **Rotation is unconditional.** The presented refresh token is single-use: it
  is revoked as part of the exchange. The agent must persist the returned token
  *before* relying on it (write-before-use).
- The window **slides**. Each refresh mints a fresh token with a full
  `JWT_REFRESH_TOKEN_EXPIRY` (default 7 days) lifetime. There is no absolute
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

## Rolling back to dev-login

Only possible while the backend is in `local` mode — `oauth` mode does not serve
dev-login at all. Clear the agent's `FAULTMAVEN_REFRESH_TOKEN` and set
`FAULTMAVEN_DEV_LOGIN_USERNAME=slack-agent`. If the backend has already cut over
to `oauth`, rolling the agent back means rolling the backend's `AUTH_MODE` back
with it.

## Blast radius

A failure of this credential degrades **Slack only**. The dashboard and Copilot
authenticate through WorkOS/PKCE and are unaffected.
