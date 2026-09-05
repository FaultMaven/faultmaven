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
API_CONTRACT_VERSION = "2.8.0"
