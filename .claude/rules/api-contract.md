---
paths:
  - "faultmaven/api/**"
  - "faultmaven/main.py"
  - "faultmaven/models/**"
  - "scripts/generation_environment.py"
  - "faultmaven/modules/*/api/**"
  - "docs/reference/api/**"
  - "scripts/generate_api_docs.py"
  - "tests/integration/api/**"
---

# API surface and the generated contract

Loaded when routes, middleware or the API reference are touched. Base URL:
`http://localhost:8090/api/v1`; Swagger UI at `http://localhost:8090/docs`.
The complete operation list is the generated `docs/reference/api/README.md`;
this file holds only the semantics a reader cannot get from a route signature.

## The reference is generated

The rule — never hand-edit `docs/reference/api/*`, regenerate with the lockfile
installed, ship the artifact in the same PR — is in root `CLAUDE.md` §Code
Quality, and how auth is derived into the spec is in §Security Rules. What
follows is why the artifact looks the way it does.

`scripts/generate_api_docs.py` empties the environment (down to
`_SYSTEM_ENVIRONMENT_KEYS`, via `scripts/generation_environment.py`) and applies
its own pinned settings, so the artifact is a function of the code rather than
of your `.env`. It documents the **maximal deployed surface** — OAuth, SSO and
`/metrics` mounted, the debug router excluded. The exclusion is the generator's
doing, not the router's: it pins `ENVIRONMENT` to `production`, which rules out
the automatic mount, and the emptied environment means the mounting flag cannot
arrive from your shell.

FastAPI and Pydantic decide how schemas are emitted, so the document depends on
their versions as well as on the code — a stale local FastAPI produces a
valid-looking artifact that CI rejects, with the diff showing up in schema shape
(`ctx`/`input` on ValidationError, `const` vs a single-value `enum`,
`contentMediaType` vs `format: binary`) rather than in routes.

The contract version clients pin is `API_CONTRACT_VERSION` in
`faultmaven/api/contract_version.py`; when and how to move it:
`docs/development/api-contract-changes.md`.

## Semantics worth knowing

| Endpoint | Semantics |
|----------|-----------|
| `POST /auth/oauth/token` | **Takes RFC 6749 §3.2 form encoding *or* JSON**, and answers errors as RFC 6749 §5.2 objects (`{"error", "error_description"}`), not `{"detail"}` — so a rejected grant is a **400** `invalid_grant`, not a 401. Same for `POST /auth/oauth/revoke` (RFC 7009). Both routes take a raw `Request` and validate by hand, because FastAPI cannot declare two body encodings on one signature; that is also why their OpenAPI `requestBody` is written out in `openapi_extra` and why they document a 400 where every other operation documents a 422 (#1150) |
| `POST /auth/dev-login` | Local-mode login as the bootstrap admin: `{"username": "admin"}` |
| `POST /cases/{case_id}/turns` | Submit a turn (multipart: query, files, pasted content) — how raw data enters a case. **One file max** per turn (`maxItems: 1`); `pasted_content` is a separate field and does not count toward it, so a turn may carry one file *and* a paste. An **empty turn** (no query, no file, no paste — a bare `@FaultMaven` in Slack) is accepted and answered with a state-aware orientation, as are whole-message greetings and "help"; that intent is server-minted, a client-sent `greeting` is re-derived from the text (contract 2.8.0). Every accepted message is charged to the turn cap (`.claude/rules/data-model.md`) |
| `POST /knowledge/scan` | Manual draft reconciliation — auto-scan was removed; ingestion is owned by the startup bootstrap |
| `GET /teams` | The caller's teams (names for share badges + share-to-team picker) |
| `POST /teams` | Create a team in the caller's enterprise; the creator is its team admin (ADR-017 D4 — any account may). Team + membership in ONE transaction; 409 `team_name_taken` for a live duplicate (the index is partial on `deleted_at IS NULL`, so a retired team frees its name) |
| `GET /teams/{team_id}/members` | The roster, readable by the team's members |
| `DELETE /teams/{team_id}/members/me` | Leave; 409 for the last admin while others remain, and the sole member leaving retires the team and closes its pending offers — live ones `revoked` by the leaver, elapsed ones `expired` with no `revoked_by`. Both decided under a row lock on the team — the rule is a read-then-write over the roster |
| `POST /teams/{team_id}/invitations` | Offer an address a place (team admin). Decided **by domain** so nothing enumerates accounts: a personal enterprise invites nobody, an address off the enterprise's domain is refused, and an address on it whose account is anchored elsewhere is refused **identically** |
| `GET /teams/{team_id}/invitations` | Every offer this team has issued and what became of it (team admin) |
| `DELETE /teams/{team_id}/invitations/{invitation_id}` | Withdraw an offer (team admin) |
| `GET /invitations` | The live offers addressed to me — by `invited_user_id`, or by my address while the offer predates my account |
| `POST /invitations/{invitation_id}/accept` | Consent. **The only call that creates a team membership** — the stamp and the membership are ONE repository transaction, because either ordering leaks (stamp-first spends a one-shot token; membership-first leaves a member nobody consented to admit when a revoke lands mid-flight); 410 once it has expired |
| `DELETE /invitations/{invitation_id}` | Decline; recorded, so the admin sees the answer |
| `GET /admin/users`, `GET /admin/users/{id}`, `POST /admin/users/{id}/roles` | Platform-admin only and confined to the caller's tenant (#1318): a user of another organization answers the same 404 an absent id does |
| `GET /admin/llm/config` | Provider status, fallback chain and `role_routing` with provenance (#1206). Read-only — role keys are not in the override allowlist. `POST /admin/llm/config/test` tests a connection |
| `GET /admin/config/status` | Resolved configuration posture: `kb_prefetch`, `first_party_consent_skip`, `features.request_protection_hardened`, `llm_retry_ladder_fits_turn_budget` |
| `GET /admin/cases` | Cross-tenant case list — platform-admin only, audited. Deployment-split (ADR-012 D9): standalone returns full summaries (`view: "full"`), cloud returns ambient metadata with no title/description (`view: "metadata"`); titles need break-glass. 403 under `TENANT_PROVIDER=multi` (RLS would make the list silently partial) |
| `GET /admin/cases/{id}`, `GET /admin/cases/{id}/messages` | Operator **content** / **transcript** read (ADR-012 D9). Standalone: standing access, audited not gated. Cloud: requires a live break-glass grant naming that case. Response envelope names how it was authorised (`access: "standing" \| "break_glass"`). Separate from `GET /cases/{id}`, which has no operator bypass. Design: `docs/architecture/security/break-glass-content-access.md` |
| `POST /admin/grants` | Mint a break-glass grant over ONE case: reason (min 20 chars) + TTL (default 60 min, max 240). No extend path — needing longer means a new grant. `GET /admin/grants` lists grants (not scoped to the caller — who holds access is the governance question); `POST /admin/grants/{id}/revoke` ends one early (idempotent; any operator may revoke any grant); `GET /admin/audit/operator-access` is the durable, append-only access trail |
| `GET /health`, `/health/dependencies`, `/health/sla`, `/health/logging`, `/health/components/{name}`, `/health/patterns`, `GET /readiness` | Health surface; `/readiness` is the Kubernetes probe |
| `GET /metrics/performance`, `/metrics/realtime`, `/metrics/alerts`, `/metrics/optimization` | Metrics surface |
| `GET /api/v1/meta/capabilities` | Backend capabilities for extension and dashboard. `GET /v1/meta/capabilities` is a deprecated alias kept for installed extensions |

## Debug endpoints

When the debug router mounts and who may call it is stated once, in root
`CLAUDE.md` §Security Rules, where `test_no_unauthenticated_operations.py`
reads it; this file deliberately carries no copy.
