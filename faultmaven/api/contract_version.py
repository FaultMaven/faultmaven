"""The version of the API contract, moved by hand.

``docs/reference/api/openapi.json`` is the contract between this API and its
clients — faultmaven-copilot, faultmaven-dashboard and faultmaven-slack-agent.
A contract changes only by agreement, so this number moves only when someone
decides to publish a change: never automatically, and never as a side effect of
shipping.

It is deliberately **not** the product version. The product ships on its own
cadence, and most releases change no route, no schema and no status code; a
number that moved with them would carry no information. ``GET /`` still reports
the product version, which is a different fact about a different thing.

Bump this in the same PR as the change it describes:

* **MINOR** — the surface grew in a way every existing client survives: a new
  endpoint, a new optional field, an additionally accepted content type.
* **MAJOR** — an existing client can break: an endpoint or field removed, a
  status code changed, a response shape replaced, a request field that was
  optional made required.
* **PATCH** — available for a re-publication that changes no structure at all.
  Not required for one: a reworded description is not something a client can
  break on, and demanding a bump for prose would train people to bump without
  reading.

``scripts/check_contract_version.py`` fails a pull request whose structural
surface differs from the base branch while this stays put, so a surface change
cannot reach clients silently. What that check deliberately does **not** do is
decide MINOR versus MAJOR: that judgement is the thing the clients are being
asked to accept, and it belongs to a person.
"""

# 3.4.0 — MINOR. Two amendments to the team-consent surface 3.1.0 published,
# found by review of #1365 after it merged.
#
# Numbered 3.4.0 for a reason that has nothing to do with what it publishes.
# Two versions were taken while this sat in review: #1369 took 3.2.0 (below),
# and #1370 takes 3.3.0 and merges first. Two different contracts must never
# share a version — a number that cannot tell two contracts apart is not doing
# its job, which is the whole reason 2.0.0 exists — so this moves rather than
# collides, and the entries stay separate. They describe unrelated surfaces,
# and merging them would lose which client has to adopt what.
#
# **3.3.0 is deliberately absent from this file right now.** It belongs to
# #1370, which has not merged yet; its entry arrives with it. A gap here is the
# honest record of two changes in flight, and inventing a placeholder to fill
# it would put a version in the changelog that nothing published.
#
#   * `InvitationResponse` gains `revoked_by` and `revoked_at`, both nullable.
#     They are what makes 3.1.0's own design claim checkable from outside:
#     `status` is `revoked` whether the team admin withdrew the offer or the
#     invitee declined it, and comparing `revoked_by` against `invited_by` is
#     the whole of the difference. A field the server writes and no client can
#     read is a record nobody keeps. New nullable fields on a response-only
#     schema: a client that ignores them renders exactly what it renders today.
#
#   * `TeamCreateRequest.name` narrows from `maxLength: 255` to `200`, which is
#     `teams.name`'s width exactly.
#
# The narrowing is the half that needs the argument, because tightening a
# request field is normally where a client breaks. It cannot break one here, on
# the 2.2.0 precedent: **no name between 201 and 255 characters has ever been
# accepted**. `teams.name` is `VARCHAR(200)`, so PostgreSQL refused every one of
# them with `StringDataRightTruncation` — an unhandled 500. A caller that sent
# one was already outside the supported contract and getting undefined
# behaviour; what changes is that it now gets a 422 naming the field. Nothing
# that worked stops working, and the surface the clients generate against gets
# narrower rather than differently shaped.
#
# One behaviour change ships with them and is NOT a schema change, so it is
# recorded here rather than shown by the diff: accepting, declining or revoking
# an invitation that has passed `expires_at` now answers **410
# `invitation_expired`** consistently. 3.1.0 answered 410 or 409
# `invitation_not_pending` depending on whether anything had listed the row
# first, and recorded an elapsed offer as `revoked` — a withdrawal nobody
# performed. Both statuses were already published for these operations, so a
# client's error handling is unchanged; what changes is that it is now
# deterministic.
#
# 3.3.0 — MINOR. A turn says which runbooks informed it (fm#1361). One
# optional response field and two new component schemas; nothing existing
# changes, so every current client survives unchanged.
#
#   * `TurnResponse.sources` — a list, defaulting to empty, of the knowledge
#     the engine put in front of the model for that turn. A client that
#     ignores it sees the response it always saw.
#   * `Source` and `SourceType` join `components`. Both already existed in the
#     API models and were simply never reachable from a published operation;
#     they are published now because `TurnResponse` references them.
#
# Each entry carries the matched excerpt as `content`, the retrieval score as
# `confidence`, and the runbook's `document_id` / `title` / `trigger` under
# `metadata`. `type` is `knowledge_base` for everything emitted today —
# `SourceType`'s other members are published because the enum is, not because
# a turn can currently return them.
#
# ⚠ **`SourceType`'s value domain does not match the Copilot's `Source.type`
# union**, and publishing the enum is what makes that a contract question
# rather than an internal detail. The FIELD NAMES agree; the values overlap on
# exactly one member of six:
#
#     published here : documentation, knowledge_base, log_file,
#                      previous_analysis, user_provided, web_search
#     copilot union  : external_api, knowledge_base, log_analysis,
#                      previous_case, system_metrics, user_input
#
# Only `knowledge_base` is emitted, so no client breaks today — but this
# document now licenses five values that client's union rejects, and a future
# emitter choosing one would break it without changing this contract again.
# The reconciliation is a two-repo decision and is deliberately NOT made here;
# until it is, a producer on this surface may emit `knowledge_base` only.
#
# The list is empty whenever the KB pre-fetch admitted nothing, which includes
# every deployment that sets `KB_PREFETCH_ENABLED=false` (fm#1360). A client
# must therefore treat absence as "no citation to show", never as an error or
# as a signal that retrieval failed. Runbooks the model fetched itself through
# the `kb_qa` tool are NOT represented: that tool returns a formatted answer
# string, so their identity does not exist at the tool boundary to publish.
#
# 3.2.0 — MINOR. `LLMProviderDetail` gains an optional, nullable
# `selected_model_priced` (#1359): whether the model this provider will
# actually call has a rate in the cost table. `false` means that provider's
# calls report $0 spend; `null` means no model is resolved yet, which is
# "nothing to say" rather than "unpriced" — an alarm that is always on is not
# read, so an uninitialised provider must not report `false`.
#
# It exists because the unpriced signal was otherwise per-CALL. A model an
# operator pins via `{PROVIDER}_MODEL` is in no `available_models` list and is
# no `default_model`, so the build-time invariants cannot see it, and the
# `llm_unpriced_calls` counter cannot fire until traffic has already been
# billed. This is the same fact, known from the resolved model before a token
# is spent, on the surface an operator reads when choosing one
# (`GET /api/v1/admin/llm/config`). It is deliberately reported rather than
# enforced: pricing is a self-declared estimate, remediable at runtime via
# `LLM_PRICING_OVERRIDES`, and an unpriced model still yields a correct
# investigation — refusing to serve on a missing rate row would take a
# deployment down the day a provider ships a model.
#
# MINOR rather than MAJOR because it is a new optional field on a
# response-only schema reached by one operator endpoint. No request shape
# changes, nothing is removed, and no existing field changes meaning — a
# client that ignores it renders exactly what it renders today. Optional
# rather than required, unlike 2.5.0 and 2.1.0, because the server genuinely
# has nothing to send for a provider it has not initialised.
#
# No client can break on it, verified by reading all three. The Dashboard
# declares the shape by hand as `LLMProvider` in `src/types/llm.ts` — a
# compile-time TypeScript interface it does not validate against, so an extra
# JSON key is inert — and additionally carries the schema in the generated
# `src/types/api.generated.ts`, where a regeneration only widens a response
# type. The Slack agent's `LLMProviderDetail` lives in the generated
# `faultmaven/api_generated.py` and is referenced nowhere outside it; pydantic
# ignores unknown fields besides. The Copilot carries it in the generated
# `packages/copilot-ui/types/api.generated.ts` and nowhere else — same
# widening-only story as the Dashboard's generated copy. (An earlier draft of
# this entry said the Copilot did not reference the schema at all. That was
# false and came from grepping only `faultmaven-copilot/src`, which is not
# where that client keeps its generated types; the MINOR call is unchanged,
# but it now rests on having actually read the file.)
#
# 3.1.0 — MINOR. Teams form by consent (ADR-017 D4). Nine operations are added
# and nothing existing is touched, so every current client survives the change
# unchanged — which is what makes this the minor bump rather than the major one
# 3.0.0 was:
#
#   * `POST /teams` — any authenticated account creates a team in its own
#     enterprise and is that team's admin;
#   * `GET /teams/{team_id}/members` — the roster, readable by its members;
#   * `DELETE /teams/{team_id}/members/me` — leave; 409 when the leaver is the
#     only admin and other members remain, and the sole member leaving
#     soft-deletes the team;
#   * `POST /teams/{team_id}/invitations`, `GET /teams/{team_id}/invitations`,
#     `DELETE /teams/{team_id}/invitations/{invitation_id}` — the team admin's
#     half: offer an address a place, see what became of every offer, withdraw
#     one;
#   * `GET /invitations`, `POST /invitations/{invitation_id}/accept`,
#     `DELETE /invitations/{invitation_id}` — the invitee's half: the offers
#     addressed to me, accept (which is the only thing on this API that creates
#     a team membership), decline.
#
# Two response schemas join `components`, `InvitationResponse` and
# `TeamMemberResponse`, plus the two request bodies `TeamCreateRequest` and
# `InvitationCreateRequest`. `TeamResponse` is unchanged — it still carries
# `enterprise_id` and no organization, exactly as 3.0.0 published it.
#
# **A refusal on this surface carries a machine-readable reason.** A 403, 404,
# 409 or 410 from these routes answers
# `{"error", "detail", "status_code", "reason"}` — the same envelope
# `ConflictError` already emits with `conflict_reason`, with the slug in
# `reason`. `detail` stays the human sentence a client renders. A client has to
# distinguish "you are already a member" from "that address cannot join a team
# in this enterprise" to say anything useful, and parsing prose for that is how
# a UI ends up wrong in a language it was not written in. The slugs are
# `enterprise_is_personal`,
# `address_outside_enterprise_domain`, `already_a_member`, `not_a_team_admin`,
# `not_found`, `invitation_expired`, `invitation_not_pending`,
# `last_admin_cannot_leave`, `single_tenant_has_no_teams` and
# `single_tenant_has_no_invitations`.
#
# Two of those slugs are deliberately ONE answer to two questions.
# `address_outside_enterprise_domain` is returned both for an address on
# another domain and for an address whose account is anchored to another
# enterprise, at the same status and with the same message, because telling
# them apart would answer "does an account exist at this address?" to anybody
# who can create a team — which is everybody. A client must not try to infer
# the difference; there is none to infer.
#
# The routes are published in every deployment and are the same shape in all of
# them, but a single-tenant deployment answers 403 with
# `single_tenant_has_no_teams` / `single_tenant_has_no_invitations`: it has one
# enterprise, one default team and one account (ADR-017 D8), so there is nobody
# to invite. Publishing them unconditionally is what keeps this document one
# contract rather than a function of a deployment's `TENANT_PROVIDER`;
# `GET /api/v1/meta/capabilities` already reports `teamSharing` so a client can
# hide the UI rather than discover the 403.
#
# `GET /teams` is unchanged in shape and in behaviour, including its empty list
# in standalone.
#
# 3.0.0 — MAJOR. The tenant a client reads off a row is the **enterprise**, not
# the organization (ADR-017). Ten schemas move, and SIX of them REMOVE a
# required field, which is why this is a major bump rather than the minor one a
# rename might suggest:
#
#   * `TeamResponse`, `AdminUserListItem`, `UserDetailResponse`,
#     `InvestigationSessionResponse` — `organization_id` → `enterprise_id`;
#   * `BreakGlassGrant` — `target_organization_id` → `target_enterprise_id`;
#   * `BreakGlassGrantRequest` — the REQUEST field `organization_id` becomes
#     `enterprise_id`, so a client that keeps sending the old name is rejected;
#   * `OperatorAccessAuditEntry` — `target_organization_id` →
#     `target_enterprise_id`, and this one is the seventh rename but NOT a
#     seventh removal of a required field: the old field was optional (a
#     cross-tenant access has no single tenant to name), so a client that read
#     it defensively survives the shape and only loses the value;
#   * `CaseSummary`, `CaseDetail`, `AdminCaseMetadata` — `enterprise_id` is
#     added as required and `organization_id` becomes optional. A case now
#     carries both: the enterprise is what the read was scoped by, and the
#     organization is billing attribution that is absent whenever nobody pays
#     for the account (which, until organization assignment ships, is every
#     account).
#
# Two QUERY parameters are renamed on the operator surfaces, and they are the
# half a schema diff does not show:
#
#   * `GET /admin/grants` — `organization_id` → `enterprise_id`;
#   * `GET /admin/audit/operator-access` — `target_organization_id` →
#     `target_enterprise_id`.
#
# An ignored query parameter is WORSE than a rejected one here, and that is why
# both old names are declared solely so they can be REFUSED with 422. FastAPI
# drops an undeclared parameter silently, so a client still sending the old name
# would get a 200 carrying every row — an unfiltered answer presented as a
# filtered one, on the two surfaces that answer "who reached that tenant's
# data". Nothing is ever served under the old name and neither appears in the
# published schema; the refusal names its replacement.
#
# No path is added or removed and no OTHER status code changes; what changed is what
# a row says about whose data it is. There is deliberately no transitional
# period in which both fields are served: a tolerated old field is what keeps a
# frontend reading it, and the whole point of moving the key is that the
# organization stops answering "who may see this?". The clients adopt by pin
# bump (faultmaven-dashboard, faultmaven-copilot, faultmaven-slack-agent,
# faultmaven-cloud), which is also the cutover for the wipe-and-reprovision this
# ships with — every session is re-established anyway, because the access and
# refresh tokens now carry an `enterprise_id` claim and a token without one is
# refused.
#
# 2.8.0 — MINOR. `POST /cases/{case_id}/turns` accepts an EMPTY turn — no
# `query`, no `files`, no `pasted_content` — and answers it with a state-aware
# orientation (where the investigation stands, what was last asked for, what
# the user can do next). It used to be a 400 the client had to swallow, which
# is how a bare `@FaultMaven` in Slack produced silence. Nothing a client sends
# today is rejected or reshaped, so every existing client survives it; a
# client that previously synthesised text for a bare mention can now send the
# turn as it is. In the same release, `intent.type = "greeting"` sent by a
# client is no longer obeyed: the server derives that intent from the text
# itself (the value stays in the enum for generated types), so a client that
# sent it gets the same answer it would have got for the text alone.
#
# 2.7.0 — MINOR. `TurnResponse` gains `investigation_turn` (nullable integer,
# #1329): how many of the case's turns so far were investigation work.
# `turn_number` keeps its meaning as the message clock and still advances on
# every exchange; an out-of-band turn (small talk, trivia, a question about
# FaultMaven itself) is now answered outside the investigation and recorded as
# such, and this is the count a client should display as "Turn N". Nullable so
# an older server that lacks it reads as "absent", not as zero. Nothing is
# removed or reshaped, so every existing client survives it unchanged; the
# clients' adoption is tracked in faultmaven-slack-agent#64,
# faultmaven-dashboard#127 and faultmaven-copilot#251.
#
# 2.6.0 — MINOR. Capability discovery gains a second path,
# `GET /api/v1/meta/capabilities`, served by the same handler as the existing
# `GET /v1/meta/capabilities`; the old path stays, and is published
# `deprecated: true` with a description naming its replacement.
#
# `/v1/meta/capabilities` was the only client-facing route outside `/api`, and
# outside `/api` is where the deployed topology stops carrying it: the ingress
# forwards `/api` (prefix), `/health` and `/metrics` to this service and
# everything else to the Dashboard SPA. A same-origin Dashboard — `VITE_API_URL=""`,
# which is the deployed default — therefore asks its own origin for
# `/v1/meta/capabilities` and is answered with the SPA's HTML, and both clients
# treat that as "no capabilities": the Copilot catches the JSON parse failure
# and serves its degraded self-hosted fallback (`src/lib/capabilities.ts`), and
# the Dashboard's `getCapabilities` rejects (`src/lib/meta/capabilities.ts`).
# The endpoint that exists to say what the deployment supports could not be
# reached by the client that most needs it.
#
# MINOR rather than MAJOR because nothing is removed or changed underneath a
# caller. The old path answers exactly what it answered before — one handler,
# two registrations, held byte-identical by
# `tests/integration/test_main_app.py::test_capabilities_is_the_same_response_under_both_paths`
# — so an extension already installed against it keeps working, which is why
# the alias is kept rather than moved. `deprecated: true` is documentation: it
# is what tells a client reading the spec which of two paths serving one
# response to write against, and OpenAPI generators emit a deprecated operation
# like any other (the Copilot and Dashboard already carry two such operations,
# POST /api/v1/auth/dev-login and /dev-register, in their generated
# `src/types/api.generated.ts`).
#
# 2.5.0 — MINOR. `EnvConfigStatusResponse` gains a required
# `personal_tenant_limits` object, and `PersonalTenantLimitsStatus` joins
# `components` (#1320, #1324). It reports the effective values of the three
# settings that bound self-service sign-up — whether an org-less SSO identity
# may provision a personal tenant, the deployment-wide hourly ceiling on that
# provisioning, and the default daily investigation-turn allowance a personal
# tenant gets. All three were reported nowhere, and all three fail silently:
# each refusal reads to the person refused as something other than a
# configured limit.
#
# MINOR rather than MAJOR because it is a new field on a response-only schema
# reached by one operator endpoint, GET /api/v1/admin/config/status. No request
# shape changes, nothing is removed, and no existing field changes meaning — a
# client that ignores it renders exactly what it renders today. Required rather
# than optional is deliberate and is a strengthened guarantee, the same shape as
# 2.1.0: the server sends it on every response, and a block that could be absent
# would read as "nothing to report", which is the failure the field exists to
# close.
#
# No client can break on it, verified by reading them. The Dashboard declares
# `EnvConfigStatus` by hand in `src/types/llm.ts` — a compile-time TypeScript
# shape it does not validate against, so an extra JSON key is inert. The Copilot
# carries the schema only in the generated `src/types/api.generated.ts`, and a
# regeneration widens a response type nothing narrows. The Slack agent's
# `EnvConfigStatusResponse` lives in the generated `faultmaven/api_generated.py`
# and is referenced nowhere outside it; pydantic ignores unknown fields besides.
#
# 2.4.0 — MINOR. `CaseReport.format` widens from `const: "markdown"` to
# `enum: ["markdown", "html"]` (#520). `reports_format_check` has admitted both
# since the clean baseline, and the repository hydrates `format=row.format`
# straight into the model — so the narrower type turned a row the database
# accepts into a 500 on READ. The document now says what the storage layer has
# always permitted.
#
# MINOR rather than MAJOR because no existing client can break on it, on two
# independent grounds. First, the server still cannot emit `html`: nothing
# writes it, so the set of values actually returned is unchanged and this is a
# published latitude rather than a behaviour change. Second, no client reads the
# field — verified by reading them: the Dashboard and Copilot carry it only in
# `src/types/api.generated.ts` (TypeScript, compile-time, and widening a
# response type is a superset none of them narrows), and the Slack agent's
# `Literal["markdown"]` lives in `faultmaven/api_generated.py` on a `CaseReport`
# that nothing outside that generated module references. A client that
# regenerates picks up the wider type and compiles unchanged.
#
# 2.3.0 — MINOR. `HypothesisSummary` gains an optional `retirement_reason`
# (#1142). The schema already carried `refutation_reason`, so a client rendering
# a terminal hypothesis could say why one was REFUTED but not why one was
# RETIRED — and retirement is the commoner end (40 retired against 8 refuted in
# the corpus), so the half that was missing was the larger one. A hypothesis the
# engine set aside having never grounded it and one it tested and abandoned
# looked identical at this seam.
#
# MINOR rather than MAJOR because it is a new nullable response field on a
# response-only schema: no request shape changes, no existing field changes
# meaning, and a client that ignores it renders exactly what it renders today.
#
# 2.2.0 — MINOR. `POST /cases/{case_id}/turns` publishes `maxItems: 1` on its
# `files` field (#694). The one-file-per-turn rule was always the supported
# contract — it is what the clarification emitter is written against — but it
# lived as convention plus client-side discipline, so nothing in the document
# said so and a new client could exceed it silently.
#
# MINOR rather than MAJOR because every existing client already sends at most
# one file, verified by reading them: the Copilot builds its payload as
# `payload.files = [selectedFile!]` (UnifiedInputBar.tsx), the Slack agent's
# `download_message_content` returns "at most one real file upload" and routes
# the rest to `skipped_names`, and the Dashboard posts no files to this route.
# No client sends a request this newly refuses. A caller that DID send two was
# already outside the supported contract and getting undefined behaviour —
# only the first attachment's failed classification was ever clarifiable.
#
# 2.1.0 — MINOR. `KnowledgeBaseDocument.scope` became REQUIRED and lost its
# `"global"` default (#1166). This is a write-side hardening surfacing in a
# read-side schema: the default meant a publish path that omitted its knowledge
# tier published to the platform corpus every tenant reads, so the field is now
# a decision every construction has to make. Existing clients survive it — the
# schema is response-only (GET /knowledge/documents/{document_id}) and the
# server already sent `scope` on every real response, because the DTO builder
# reads it off the row. A response field going from optional-with-default to
# required is a strengthened guarantee, not a new obligation on the caller.
#
# 2.0.1 — PATCH. `revoke()` was annotated `-> Any` while it still returned a
# JSONResponse for errors; once those moved to a raised exception the
# annotation was merely inaccurate, and it erased `type: object` from the
# documented 200 response. Nothing on the wire changes, so no client can break
# on this and none needs to adopt it urgently — it restores what the document
# says about a response that never varied. Found by the advisory
# breaking-change report on its first real run, and by nothing else.
#
# 2.0.0 — MAJOR, and it publishes a change that already shipped. #1152 moved
# `invalid_grant` from 401 to 400, replaced `{"detail": ...}` with the RFC 6749
# §5.2 error object, and dropped `TokenRequest`/`RevokeRequest` from
# `components` on POST /auth/oauth/token and /auth/oauth/revoke. It merged
# before this machinery existed, so main's contract diverged from the one the
# clients pinned while both still called themselves 1.0.0 — a version that
# cannot tell two contracts apart is not doing its job. The first act of the
# version is therefore to give the contract on main an identity distinct from
# the 1.0.0 the clients are written against.
API_CONTRACT_VERSION = "3.4.0"
