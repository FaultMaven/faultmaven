---
paths:
  - "faultmaven/modules/auth/**"
  - "faultmaven/modules/case/**"
  - "faultmaven/infrastructure/persistence/**"
  - "faultmaven/cli/**"
  - "faultmaven/modules/agent/domain/services/investigation_service/**"
  - "faultmaven/infrastructure/protection/tenant_turn_cap.py"
  - "faultmaven/config/constants.py"
  - "faultmaven/bootstrap/data_init.py"
  - "faultmaven/api/middleware/tenant_scope.py"
  - "faultmaven/api/middleware/principal.py"
  - "faultmaven/api/routes/**"
  - "alembic/**"
  - "tests/unit/modules/auth/**"
  - "tests/unit/modules/case/**"
  - "tests/unit/cli/**"
  - "tests/integration/modules/**"
---

# Auth, tenancy, sharing and usage accounting

Loaded when auth, case, persistence, CLI or migration code is touched. Table
shapes: `docs/architecture/data-and-storage/schemas/` and the ER diagram;
tables by domain: `docs/reference/database/README.md`. Auth design:
`docs/architecture/security/iam-design.md`.

## Auth modes

| Mode | Algorithm | Use case | Configuration |
|------|-----------|----------|---------------|
| `local` | HS256 (symmetric) | Self-hosted, single-user | `AUTH_MODE=local`, `JWT_SECRET_KEY` |
| `oauth` | RS256 (asymmetric) | Cloud, multi-user, browser extension | `AUTH_MODE=oauth` **and** `OAUTH_ENABLED=true` (startup refuses one without the other; the flag mounts the OAuth router in `main.py`), RSA key pair (`JWT_PRIVATE_KEY_PATH`, `JWT_PUBLIC_KEY_PATH`; `python scripts/generate_oauth_keys.py`) |

Both modes mint the same token shape (`sub`, `username`, `email`, `roles`,
`scopes`, `exp`/`iat`, `iss=faultmaven`, `aud=faultmaven-api`, `jti`, `type`,
`auth_mode`) — see `docs/architecture/security/iam-design.md` §"Unified JWT
Format". Revocation is JTI-based. Expiry (`JWT_ACCESS_TOKEN_EXPIRY_MINUTES`,
default 15, max 1440; `JWT_REFRESH_TOKEN_EXPIRY_DAYS`, default 7, max 90) is a
single source effective in **both** modes; out-of-range values and the retired
`JWT_*_EXPIRE_*` spelling fail at startup
(`docs/architecture/security/jwt-expiry-configuration.md`).

- `OAUTH_REDIRECT_URI_PATTERNS` — where an authorization code may be delivered. Default is the two `identity.launchWebAuthFlow` hosts, `^https://[a-p]{32}\.chromiumapp\.org/?$` (Chrome) and `^https://[a-f0-9]{40}\.extensions\.allizom\.org/?$` (Firefox — a 40-hex digest, **not** the `moz-extension` UUID). The old `chrome-extension://…/callback.html` forms were removed from the default in #1065; a deployment serving pre-#192 extension builds must re-add them. Both patterns wildcard the extension id, so they identify *an* extension, not *ours*.
- `OAUTH_FIRST_PARTY_CLIENTS` + `OAUTH_FIRST_PARTY_REDIRECT_PATTERNS` — which clients skip the consent screen. **Both are required and only the redirect proves anything**: `client_id` is caller-supplied, so the clients list narrows the field but identifies nobody. The redirect list defaults to `[]`, so **nothing skips consent** until a deployment pins its published extension id (#1066). A wrong consent skip fails silently (nothing appears), so `GET /admin/config/status` reports `first_party_consent_skip` to distinguish "never activated" from "working". All three parse as JSON lists; a bare value fails at startup.

## Auth module map

```
modules/auth/
├── contracts.py                    # Public DTOs (UserDTO, AuthTokenDTO, SessionDTO)
├── api/
│   ├── auth.py                     # Login, register, dev-login, token refresh
│   ├── oauth.py                    # OAuth 2.0 flow with PKCE
│   ├── session.py                  # Session management
│   ├── teams.py                    # /teams — list, create, roster, invite, revoke, leave
│   ├── invitations.py              # /invitations — the invitee's half: list, accept, decline
│   └── rate_limiting.py            # Auth-specific rate limiting
├── domain/
│   ├── models/                     # User, Session, RBAC, Organization models
│   └── services/
│       ├── auth_service.py         # Core authentication logic
│       ├── auth_session_service.py # Session management service
│       ├── oauth_service.py        # OAuth 2.0 implementation
│       ├── sso_login_service.py    # Hosted SSO (WorkOS) login path
│       ├── jwt_token_generator.py  # RS256/HS256 token generation
│       ├── user_service.py         # User CRUD operations
│       ├── organization_membership_service.py  # Billing membership
│       ├── service_account_provisioning.py     # fm-provision-service-account
│       └── team_service.py         # Teams: create, the invitation rule, accept, leave
└── infrastructure/
    ├── repositories/               # User, session, OAuth code, team, org repositories
    ├── stores/                     # Redis session stores (FakeRedis for local), token revocation
    └── metrics/                    # OAuth metrics tracking
```

## Case persistence map

```
modules/case/infrastructure/
├── case_repository.py                    # Abstract base repository
├── sqlite_case_repository/               # SQLite implementation (default): repository.py, rows.py, loading.py, saving.py
├── postgresql_hybrid_case_repository/    # PostgreSQL implementation: repository.py (class), rows.py, loading.py, saving.py
├── sessionless_case_repository.py        # Sessionless repository variant
├── case_scope.py                         # Read-scope SQL: the owned ∪ shared-to-my-teams visible-id allowlist (+ driven_only = access=write)
├── case_driver_sql.py                    # The driver writes (ADR-020): versioned reassign / conditional release + their audit row, both dialects
├── case_metadata_reader.py               # PostgreSQL-only cross-enterprise operator list (multi): reads the SECURITY DEFINER metadata functions
└── created_bounds.py                     # Creation-date window bounds, normalized to UTC, one place for every repository
infrastructure/persistence/
├── models.py                             # SQLAlchemy ORM models — every table
├── db_errors.py                          # SQLSTATE / driver error out of a SQLAlchemy DBAPIError, one place
├── investigation_session_repository.py   # Session management
└── case_vector_store.py                  # Vector storage for cases
```

The removed `agent_executions` / `agent_tool_calls` tables, and what
`investigation_sessions.total_agent_executions` is instead (#1350):
`docs/reference/database/README.md` §Tables by domain.

## Tenancy (ADR-017)

ADR-017 supersedes ADR-013's "Organization is the hard-isolation boundary":
three tiers answer three separate questions.

- `enterprises` **isolates** — PostgreSQL RLS keys on `enterprise_id`, denormalized NOT NULL onto every tenant-scoped table (`users.enterprise_id` included) and re-keyed via the `app.current_enterprise_id` session GUC (`app.current_org_id` no longer exists).
- `organizations` **bills** — `organizations.enterprise_id` NOT NULL FK, but `organization_id` on data rows is nullable billing attribution (`ON DELETE SET NULL`) stamped from the actor's organization at write time and never a visibility predicate; org roles (`admin`/`member`/`viewer`) stay the organization's *management* vocabulary and gate no data.
- `teams` **share** — parented by `teams.enterprise_id` (there is no `teams.organization_id`), so one team may span organizations of the same enterprise; membership requires the same enterprise.
- A team **forms by consent** (ADR-017 D4): any account may create one and is its team admin, the admin offers an address a place in `team_invitations`, and the invitee's own accept is the only call that writes a `team_members` row — a pending invitation grants nothing.
- Who may be offered a place is decided **by email domain**, before any account lookup, so the invitation endpoint is not an account-existence oracle: a personal enterprise (`domain IS NULL`) invites nobody, an address off the enterprise's domain is refused, and an address on its own domain whose account is anchored to another enterprise is refused with the *same* status and body.
- An offer to an address with no account is created unresolved and is stamped with the account id by the SSO sign-up path when — and only when — that address lands in the issuing enterprise; expiry is lazy (`TEAM_INVITATION_TTL_DAYS`, default 14; no sweeper) and settled through one reader, so accept/decline/revoke of an elapsed offer all answer **410** and record `expired`, never a withdrawal nobody made.
- ‼ The address key is `strip().lower()` because that is the index `users.email` is looked up by (`func.lower`) — case-folding would miss accounts where the two differ and silently skip the anchored-elsewhere refusal.
- A 404 on this surface carries **no** reason slug (the house `NotFoundError` envelope); only the 403/409/410 family does.
- ‼ Expiry is settled **after** the entitlement check, never inside the shared read — settling first let any account in the enterprise mutate a stranger's invitation and be told 404 for it.
- ‼ `team_service is None` means **single-tenant and nothing else**: it is read deployment-wide as "no team sharing here" (case read allowlist, KB visibility, engine, `GET /teams`), so a missing dependency under multi is fatal at startup rather than `None`.
- ‼ Every write on `ITeamRepository` carries `enterprise_id`, not just the reads.
- Standalone seeds one enterprise (`STANDALONE_ENTERPRISE_ID`, `…0002`) and one default team, and **no organization row** (`STANDALONE_ORG_ID` is deleted from `constants.py`).
- Sign-up (`SSO_JIT_PERSONAL_TENANT_ENABLED`, OFF by default) derives the email domain: a `PERSONAL_EMAIL_DOMAINS` match yields a private enterprise per account; any other domain yields, or joins, that domain's enterprise (`enterprises.domain`) — a sign-up creates NO organization and NO team.
- `sso_org_mappings` maps an IdP organization to an **enterprise** (not an organization); `sso_personal_orgs` is deleted, replaced by `sso_personal_enterprises` (keyed on `(provider, subject)` — a subject handle is unique only within an IdP). Design: `docs/architecture/security/sso-org-mapping.md`.
- Account kinds are exactly `individual` and `service` (`users.account_kind`) — a team is a group of accounts, never an account, and the vocabulary that called one a team is retired. Which integration a service account serves is the separate `users.service_channel` column (e.g. `'slack'`).

**Sharing:** `resource_shares` — polymorphic `(resource_type, resource_id, scope_type, scope_id)` association (ADR-013 §D4, unchanged by ADR-017 — teams still share by consent, just parented by the enterprise now). Single source of truth for team visibility of runbooks/cases/drafts; replaced the nullable `team_id` columns on `cases`/`knowledge_items`/`conversion_jobs`. v1 `scope_type=team`; `organization` reserved (D4a). Retrieval resolves it to a visible-id allowlist in SQL; ChromaDB metadata never carries team state.

**The case driver (ADR-020, #1898).** A case has a **creator** (`cases.user_id`, unchanged) and a **driver** (`cases.driver_id`, nullable FK to `users`, `ON DELETE SET NULL`). NULL means the creator drives; the **effective driver** is `COALESCE(driver_id, user_id)` — `Case.effective_driver_id` in Python, `case_scope_where(..., driven_only=True)` in SQL — and on the wire `CaseSummary`/`CaseDetail.driver_id` is always the effective one, beside `creator_display_name`/`driver_display_name` (display names only, never email). Every reader (creator ∪ shared-to-my-teams) views a case; the driver holds the investigation writes; the creator holds governance. Driving grants NO visibility: no read path keys on `driver_id`.

- ‼ **One resolver, three arms** — `CaseService._may_access` (behind `get_case` and `_resolve_case_for_access`): read (no flag); `driver_only=True` = the effective driver AND a reader, so a driver who lost every share is refused even before a release reaches the row; `creator_only=True` = the creator. A falsy `user_id` admits (an internal caller); every route passes the caller.
- **Driver** (`driver_only`, refused with the **404** an absent case gets): `PUT /cases/{id}` (and `update_case`, whose versioned retry re-checks the driver on every fresh load), `POST /cases/{id}/title` (before any title is generated) and the auto-title, resume (`POST /cases/sessions/{sid}/resume/{case_id}` and `link_session_to_case`), the session writes (`require_case_access` picks the arm by HTTP method), `DELETE /cases/{id}/data/{id}` (a stub), `POST /cases/{id}/reports`, `POST /reports/generate`, report edit/delete/link-case, and `POST /cases/{id}/close` (`close_case`, which re-checks the driver inside its retry).
- **Driver, refused with 403**: the **turn** and **evidence reclassification** (route and in-turn tool), both in `InvestigationService`. The turn resolves through the READ allowlist at the route, and `_verify_access_and_reserve` refuses anyone but the effective driver **after** the `Idempotency-Key` replay (#1888): receipts are keyed on the caller, so a gate ahead of the replay would refuse the retry of a turn the caller committed before the case was reassigned away from them. It runs before the turn cap is charged — and the cap charges the CALLER's billing subject, so the driver pays for the turns they drive. Do not move it to the route. Reclassification admits an assigned driver only with `caller_reads_case` (the PATCH route resolves it through the read allowlist) or inside a turn.
- **Creator** (governance): `DELETE /cases/{id}` (403 to any other reader, the driver included — 204 would claim a deletion), share and unshare (`share_case_with_team`/`unshare_case_from_team`, 403).
- **Creator or effective driver**: `PUT /cases/{id}/driver` and `GET /cases/{id}/driver-candidates` (`case_driver.CaseDriverMixin`). A non-reader gets 404; another reader 403; a target who is not a candidate 422; a terminal case 409 `CASE_TERMINAL`; a version race lost three times 409 `CASE_VERSION_CONFLICT`. Candidates = the creator (any account kind) + the ACTIVE INDIVIDUAL members of the teams the case is shared with, in its enterprise (a service account would leave no human able to write the case). Standalone has no teams, so the creator is the only candidate and any other target is the same 422 — no special refusal. Naming the creator stores NULL; naming the current driver writes nothing.
- ‼ **Every driver change is versioned and audited in one transaction** — `ICaseRepository.reassign_driver` (compare-and-swap on `version`) / `release_driver` (conditional on the stored driver), both bumping `version` and writing a `user_audit_log` row `event_type=case_driver_changed`, `event_category=authorization`, `details={from_driver_id, to_driver_id, reason}` (effective ids; reasons `reassigned | unshared | left_team | deactivated | reanchored | creator_reassigned`). The bump is what makes a turn in flight fail with 409 `CASE_VERSION_CONFLICT` — a reassignment during a turn is NOT refused; the rare lost turn spend is accepted. ‼ The full-row save NEVER writes `driver_id` (both SQL repositories' UPDATE omits it; the in-memory one keeps the stored value) and neither does the unversioned `update_metadata_fields` channel: its one writer is `reassign_driver`/`release_driver` (plus `fm-reassign-cases`' own UPDATE), so no turn's save can write a driver back. Loads carry it.
- ‼ **Release points (ADR-020 D3): the driver must always be able to read the case.** An operation that would cost the driver their LAST read path hands the case back to its creator FIRST — before its own write, because that write commits in a store the case store cannot share a transaction with: unshare (`release_driver_before_unshare`), leaving a team (`TeamService.leave_team` → the case contract's `ICaseDriverRelease`, which also covers a team retired by its last member), deactivation (`UserService.deactivate_user`, the admin path included), `fm-personal-tenant reanchor` (a step ahead of the anchor move) and `fm-reassign-cases` (clears `driver_id` in the owner swap's own UPDATE: the new creator drives). The ports are bound by the composition root (`bind_case_driver_release`) because both services are built before the case service. **Release-first fails safe, and that is its residual:** if the write then fails or is refused — e.g. a last-admin leave answered 409 — the case went back to its creator needlessly, audited, and can be reassigned. A release AFTER the write is never allowed. Security never depends on these hooks; liveness does.
- `GET /cases?access=write` (and `CaseSearchRequest.access`) lists the cases the caller DRIVES — the extension's list (ADR-020 D8) — ANDed onto the read scope in the same WHERE clause, so `total_count` agrees with the page. `access=read` (default) is every case the caller can read.
- **Known refusal-shape gaps, unchanged by the split** (documented, not fixed): `IdempotencyMiddleware` replays a non-turn POST's committed response before its gate (keyed on the caller, so a former driver replaying their own response learns nothing new); reclassification answers 403 to a same-enterprise non-reader without a read pre-check (it only confirms the id names a case in the caller's enterprise); share/unshare answer 403 for an absent case (`ValidationException`); `PUT /cases/{id}` with an empty body answers 400 before the gate; deleting a runbook report answers 403 to the driver; a falsy `user_id` bypasses the resolver.
- On a **terminal** case, a reader's turn carrying a file, a paste, `status_transition` or `file_reclassification` gets **409 `CASE_TERMINAL`** before the service's 403, because the route's terminal gates deliberately sit after the replay too; it leaks nothing reading the case does not already show.
- ‼ **KB retrieval follows the driver (ADR-020 D9, owner amendment 2026-10-09)**: a case retrieves with its effective driver's knowledge (global ∪ the driver's personal KB ∪ the driver's teams' runbooks); the model's answer text is accepted disclosure; stored `sources` are gated per viewer at read time (#1920). The cached pre-fetch (`case.kb_context`) carries `kb_context_origin = {driver_id, query, trigger}`; context fetched for anyone but the current effective driver is hidden from every reader (`kb_push.visible_kb_context` → `kb_context_is_stale`) and is re-run with its stored query at the next turn's start (`KbPrefetcher.refresh_for_driver`), or cleared if it cannot be. Checked at the consumer, so no driver-changing write (reassign, every release, `fm-reassign-cases`, the FK's `SET NULL`) has to know the field exists. Residual, accepted: excerpts already in a prompt stay until the next fetch.
- Actor attribution (ADR-020 D6): evidence `collected_by`, the confirming user of a pending terminal transition (`triggered_by`) and the `solution_applied_by` fallback record the effective driver (on a turn, the driver IS the principal). The runbook-conversion destination, the title sequence, the active-case limit, the Slack auto-share's `created_by` and the `organization_id` stamp stay with the creator. Slack cases: the workspace's service account creates and drives them, so no human can reassign one; only `fm-reassign-cases` moves it.

## Usage accounting and the turn cap

`turn_usage` is keyed on `(enterprise_id, billing_subject_kind, billing_subject_id, usage_date)` and holds the investigation turns accepted that day. The enterprise leads the key because RLS scopes the table on it: a conflict target that omits it can resolve to a row the inserting session cannot see, and `ON CONFLICT DO UPDATE` then raises rather than counting — which after a same-day re-anchor refused every remaining turn of the UTC day.

It is the ledger the per-tenant turn cap (`TENANT_DAILY_TURN_CAP`, default 30/UTC day) reserves against (ADR-016 D5.3, re-keyed to a billing subject by ADR-017 D5): the **billing subject** is the account's organization when it has one, and the account itself when it does not — "personal" is no longer a flag or a separate table, just the state of having no organization. The reservation is a single `INSERT … ON CONFLICT … DO UPDATE … WHERE turn_count < :cap RETURNING`, so a refused turn increments nothing. Rows are written for every billing subject, capped or not — an organization is never refused, but its counts are what the default is tuned against. The cap itself is `organizations.daily_turn_cap` (NULL = deployment policy, 0 = uncapped, N = N/day; only an organization is writable — an account in no organization has no row to carry an override), written by `fm-set-turn-cap --enterprise-id ... --organization-id ...` (or `--account-id`, read-only) and read on every turn. Charged inside `InvestigationService.process_turn`, after the case load and access check — so a 404/409/422 and a cross-tenant probe cost nothing. Single-tenant deployments are never capped and never touch the ledger.

**Every message pays, asides included** (#1329: the cap bounds compute, not diagnostic progress, and a classifier-keyed exemption would be a free channel). What an out-of-band aside (small talk, trivia, a question about FaultMaven itself) changes is the route, not the charge: it is answered from a small prompt outside the engine, recorded with `TurnOutcome.OUT_OF_BAND`, excluded from every investigative-turn count, and hidden from every history fidelity; the message clock (`current_turn`) still advances, and the investigation turn is what clients should display — `TurnResponse.investigation_turn` on the reply to a submitted turn, `Message.investigation_turn` per conversation row (null on a `system` notice, which owns no turn), and `investigation_turn` on every schema that publishes a turn FOR DISPLAY — the case reads (`CaseUIResponse_*`, `CaseSummary`, `CaseDetail`, `AdminCaseMetadata`), the conversation rows (`Message`), and the evidence and uploaded-file rows (`EvidenceDetailsResponse`, `DerivedEvidenceSummary`, `SourceFileReference`, `UploadedFileMetadata`, `UploadedFileDetailsResponse`, `case_ui.EvidenceSummary`) — #1387/#1391, contract 3.6.0. The `*_at_turn` / `turn_number` / `current_turn` fields keep their meaning as the MESSAGE clock and are what anchors and jump-to-turn are keyed on. The row field is an **ordinal** (the clock at that row minus the asides at or before it), the other two are the case's running **count**; they are one formula (`Case.investigation_turn_at`) read at different points, and they agree on the newest row, which is what keeps a live label and a reloaded one equal. The clock is still what ADDRESSES a turn — conversation anchors, evidence `uploaded_at_turn` — so display the ordinal and address by the clock.

**The LLM usage ledger is a different table with the opposite failure contract** (#640). `llm_usage_daily` (keyed on enterprise, UTC day, billing subject, actor, provider, model, outcome) and `llm_turn_spend` (one row per engine turn, deleted with its case) record the spend of every billed call, in both deployment modes, after the call — and a failed write never fails anything: it is counted on `llm_usage_unpersisted_calls_total{reason}`. The payer comes from the same `billing_subject_for` the cap charges with, plus a third kind, `none`, because metering must not refuse what the cap would. No key column is nullable (`''` for no actor or no subject) and there is no FK on the actor. Do not fold one ledger into the other, and do not expect them to reconcile. Semantics: `docs/operations/monitoring/llm-cost-observability.md` §"The usage ledger".

## Operator entrypoints

Everything an operator runs against a deployment is an `fm-*` console entrypoint
in `faultmaven/cli/` (`[project.scripts]`), shipped with the installed package —
`scripts/` is excluded from the wheel and never COPYed into the image (#887).
Inventory and semantics: `docs/operations/operator-cli.md`.
