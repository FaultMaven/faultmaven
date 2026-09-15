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

# 5.0.0 — MAJOR. Five published operations that do nothing they claim are
# REMOVED from the session router, along with the request model one of them
# required (#1425, #1431). One bump, because they are one defect wearing five
# hats, and a client should adopt the answer once.
#
#   * `GET /api/v1/sessions/{session_id}/cases`. CLAIMED: this session's cases,
#     narrowed by `include_empty`, `include_terminal` and `include_deleted`.
#     DID: `case_service.list_user_cases(current_user.user_id, filters)` — the
#     bearer's cases, under its own comment reading `Architecture: Session →
#     User → User's Cases (indirect relationship)`. The same rows
#     `GET /api/v1/cases` returns, minus `state`, `source`, `team_id` and the
#     creation-date window, which this route never accepted.
#   * `POST /api/v1/sessions/{session_id}/restore`. CLAIMED: `"status":
#     "restored"`, a message naming the restoration type, and an
#     `items_restored` count. DID: loaded the session, 404'd if absent, mutated
#     NOTHING, and reported success (#1425).
#   * `GET /api/v1/sessions/{session_id}/recovery-info`. CLAIMED:
#     `can_restore: true`, `backup_available: true`, `data_integrity: "good"`,
#     three `restoration_options` all true. DID: returned those as literals.
#     There is no backup.
#   * `GET /api/v1/sessions/{session_id}/stats`. CLAIMED: per-session query,
#     upload, heartbeat and stats-request counts, plus a latest confidence
#     score. DID: counted `session.case_history`, which is always empty (below).
#   * `POST /api/v1/sessions/cleanup`, BOTH definitions of it — the route was
#     declared twice in one file, and that is its own entry below.
#   * `SessionRestoreRequest`, the schema `restore` took. Its `restore_point`
#     was `Field(..., min_length=1)`: REQUIRED, validated, and read by nothing.
#
# `restore` AND `stats` REST ON A LIST THAT IS ALWAYS EMPTY, and this is what
# turns #1425 from a dead field into a dead endpoint. `AuthSession.case_history`
# and `.data_uploads` are initialised to `[]` by the Redis store and NOTHING IN
# THE REPOSITORY EVER APPENDS TO EITHER. There is no `add_case_history` and no
# `add_data_upload` anywhere — the two call sites that reach for
# `session_manager.add_case_history` guard with `hasattr` and have always found
# nothing — and `AuthSessionService.update_session` names both in its
# `forbidden_fields` set, so a caller cannot write them either. The consequences
# are worse than the issues report: `items_restored` was not "a count of items
# the session already had", it was `{0, 0}` on every call in the product's
# history, and every statistic `/stats` published was likewise always zero. The
# endpoints did not degrade; they never worked.
#
# THE ALWAYS-ZERO REPORTING DID NOT STOP AT `/stats`, and the sweep was not
# finished until it did. `GET /sessions/{session_id}` published
# `metadata.data_uploads_count` and `metadata.case_history_count`, and
# `GET /sessions` the same pair per row — `len()` of the same never-appended
# lists, so always 0, on two operations this change KEEPS. Removing `/stats`
# for always-zero statistics while leaving those two would have left a client
# the same wrong conclusion on an endpoint it has more reason to call. They are
# gone, and no published schema moves with them: `SessionResponse.metadata` is
# `Optional[Dict[str, Any]]` and the list route declares no `response_model`.
# The dead `add_case_history` block went from `session_heartbeat` for the same
# reason — the guard has always been False, and it was doing the work on the
# busiest route on the router.
#
# WHY DELETE RATHER THAN RETURN 501, which #1425 offered as its third option.
# 501 keeps a promise alive, so it is worth something only if the promise has a
# referent. Session restoration has none. The nearest thing in the codebase is
# `CaseCheckpoint`, and it is a different noun: a snapshot of a CASE, keyed
# `{case_id}:turn:{n}:{trigger}`, with no session dimension at all. It is
# written (three call sites in `milestone_engine`) and never read — `get_checkpoint`
# and `get_checkpoints` have no caller outside the repositories that implement
# them — so it could not restore a session even if a session were the thing it
# snapshotted. A 501 here would be a promise pointing at nothing, which is the
# same defect this entry is about with a different status code.
#
# THE CASES ROUTE HAD THREE FAULTS, NOT ONE, and each alone would have been a
# reason to change it:
#
#   1. `include_terminal` and `include_deleted` were handed to `CaseListFilter`,
#      which declares neither and sets no `model_config`, so Pydantic's default
#      `extra='ignore'` dropped both without a word (#1431) — the identical
#      mechanism as 4.0.0's `include_archived`, one route over. The seventh
#      recorded instance of this defect class.
#   2. It never compared `session.user_id` to `current_user.user_id`. It 404s on
#      a session that does not exist and answers for one belonging to somebody
#      else — harmlessly, because it ignores the session entirely, which is the
#      only reason this is a design smell rather than a disclosure.
#   3. Its outer handler turned EVERY exception into `200 []` with
#      `X-Total-Count: 0`, "for robustness per OpenAPI requirement". A failed
#      case lookup was indistinguishable from an empty account.
#
# It also imported `SessionCasesResponse` and never used it — the route carried
# no `response_model` and returned a bare `JSONResponse` list, so the one schema
# describing its own shape was dead on arrival. That model is deleted with it;
# it had no other importer and was never in the published document.
#
# `POST /api/v1/sessions/cleanup` WAS DEFINED TWICE, and the two halves of the
# system disagreed about which one existed. Starlette matches in registration
# order, so the FIRST definition served every request; FastAPI's OpenAPI
# generator writes each path once and the LAST writer wins, so the document
# published the SECOND — its `operationId` (`cleanup_expired_sessions_v2`), its
# summary and its description ("admin/testing endpoint ... In production, this
# runs automatically every 30 minutes"). The served handler is the other one,
# and it returns a `timestamp` the documented one does not. A client generating
# an operation from this contract named a handler that never ran.
#
# `api-contract-drift` COULD NOT SEE THAT, and the reason is structural rather
# than an oversight: the check regenerates the document and diffs it, and the
# document IS `app.openapi()`. Both sides of the comparison come from the same
# generator, which has the same last-writer-wins blind spot, so a shadowed route
# is invisible to it by construction. `ruff` does flag the shadowing — `F811
# Redefinition of unused 'cleanup_expired_sessions'` — but CI runs
# `ruff check --select E9,F63,F7,F82,I`, and F811 is not in that set. The rule
# that would have caught it was there all along, switched off.
#
# BOTH DEFINITIONS GO, rather than one surviving. The operation is
# unauthenticated, has no client caller in any of the three repositories, and
# deletes nothing Redis has not already dropped: sessions are stored with a TTL
# (`redis_session_store`, `ex=ttl`), and `cleanup_expired_sessions` walks the
# store deleting rows whose `expires_at` has passed — rows the TTL has evicted.
# Keeping one would mean choosing which of two identical bodies is canonical and
# publishing an unauthenticated maintenance verb to justify the choice.
#
# WHAT REMOVING THEM EXPOSED, and it is not a regression this change caused.
# `AuthSessionService.cleanup_expired_sessions` now has NO CALLER — the two
# routes were the only ones. The method was kept rather than deleted with them,
# because the missing caller is the actual defect: `SessionSettings`
# declares `cleanup_interval_minutes` (default 15, bound to
# `SESSION_CLEANUP_INTERVAL_MINUTES`) and **no scheduler, task or loop reads
# it**. Its only reader is a `field_validator` that REFUSES TO START the app
# when the interval exceeds `SESSION_TIMEOUT_MINUTES`, on the stated ground
# that "Cleanup should run at least as often as session expiration" — a
# startup constraint enforcing a cadence for a pass nothing schedules, so an
# operator who tunes this knob can be denied a boot over a loop that does not
# exist. The deleted v2 route's own description asserted "In production,
# this runs automatically every 30 minutes", which was false in two ways at
# once. The project has the machinery (`infrastructure/tasks/case_cleanup.py`
# runs a `BackgroundScheduler` for cases); it was never wired to sessions, and
# `cleanup_inactive_sessions` beside it runs only from the lifespan SHUTDOWN.
# Redis expiry covers the shipped configuration, which is why this is a gap
# rather than an outage — a non-Redis store would never expire a session. The
# scheduler is deliberately NOT built here: it is a behaviour change with its
# own blast radius and belongs in its own change, and the method stays so that
# change has something to call. This is the same defect class as the rest of
# this entry, one level up: declared in configuration, accepted, never applied.
#
# THE CLIENT-FIRST STEP WAS ALREADY SATISFIED, and here is the evidence rather
# than the assertion. `docs/development/api-contract-changes.md` orders a REMOVE
# client-first, so all three client repositories were grepped for every removed
# path, for `SessionRestoreRequest`, and for `restore_point` / `recovery-info` /
# `include_terminal` / `include_deleted`, excluding the generated artifacts —
# `src/types/api.generated.ts` and `faultmaven/api_generated.py` are outputs of
# this contract, not callers of it:
#
#   * faultmaven-dashboard makes NO hand-written call to `/api/v1/sessions` at
#     all. Its only trace of any removed operation is the generated types file.
#   * faultmaven-copilot calls exactly two session routes, both of which survive:
#     `POST /api/v1/sessions` (`packages/copilot-ui/lib/session/client-session-manager.ts`)
#     and `POST /api/v1/sessions/{id}/heartbeat`
#     (`packages/copilot-ui/lib/api/services/session-service.ts`). Its e2e mock,
#     playground stub and API tests mirror those two and nothing else.
#   * faultmaven-slack-agent makes no session call. `SessionRestoreRequest`
#     exists in its `api_generated.py` and nowhere else.
#
# WHAT THIS EVIDENCE IS NOT, stated deliberately rather than left implied.
# `docs/development/api-contract-changes.md` sets a higher bar for a REMOVE
# than a grep: "Count arrivals of the old form — a Prometheus counter on
# requests in the legacy shape — and contract when it reads zero across a full
# deploy cycle of every client. 'Nobody should still be using it' is not
# evidence." That was NOT done here, and the gap is widest exactly where it
# matters: FOUR of the five removed operations were unauthenticated
# (`restore`, `recovery-info`, `stats`, `cleanup`), so their plausible callers
# — an operator's cron, a monitoring probe, anything a self-hosted admin wired
# to a published maintenance verb — are precisely the population a grep of
# three first-party repositories cannot see.
#
# The judgement is that measurement would not change the answer, because the
# thing being measured cannot work: `restore` mutates nothing, `stats` and the
# `items_restored` count read lists that are always empty, `recovery-info`
# returns literals, and `cleanup` deletes what Redis has already evicted. A
# counter proving somebody calls `/restore` would establish that they are being
# lied to more often, not that the endpoint should stay. That is the argument
# for removing without instrumenting, and it is recorded here so a reader can
# disagree with the reasoning rather than discover the rule was skipped.
#
# So each client's adoption PR is a regeneration: five path entries and one
# schema leave the typed surface, and no hand-written line changes. Both
# TypeScript clients derive their filter types from the generated `operations`
# type, which is what makes a removed parameter a COMPILER error there rather
# than a silent no-op — the property 4.0.0 relied on and the reason MAJOR is the
# right call even where nothing on the wire moves.
#
# ONE INTERNAL FIX SHIPS BESIDE THE REMOVAL, and it is the same defect with its
# sign reversed. `MinimalCaseService` — the stand-in the DI container falls back
# to when no case repository is available, so every line of it runs in production
# the moment the repository is missing — filtered on
# `getattr(filters, "include_deleted", False)` and
# `getattr(filters, "include_terminal", False)`. `CaseListFilter` declares
# neither, so neither `getattr` could ever see anything but its default: the
# stand-in dropped every RESOLVED and CLOSED case UNCONDITIONALLY, where
# `CaseService.list_user_cases` excludes no terminal state at all. Its
# no-filters branch said the same thing without a filter object to blame it on,
# narrowing to INQUIRY/INVESTIGATING and dropping empty cases where the real
# service passes `state=None` and `include_empty=True`. Both are gone, from
# `list_user_cases` and `count_user_cases` together so the page and its count
# cannot disagree, and
# `tests/unit/container/test_minimal_case_service_terminal_parity.py` pins it.
# Not contract surface — `MinimalCaseService` is not published — but the same
# sweep found it, and a filter nobody asked for is the same lie as a filter
# nobody applies.
#
# ONE DOCUMENT WAS ARGUING WITH ITSELF and is corrected here.
# `docs/architecture/case-and-session/case-and-session-concepts.md` listed
# "Cases as session sub-resources" as **ELIMINATED** in its migration section
# while, 500 lines earlier, requiring that
# `GET /api/v1/sessions/{session_id}/cases` and `GET /api/v1/cases` "return
# identical results for the same user", with client code and a test checklist to
# match. The first statement is now true, and the second is deleted: a route
# whose entire specification is "answer what another route answers" was the
# argument for removing it.
#
# MAJOR because published operations DISAPPEAR. Five paths and one schema leave
# `docs/reference/api/openapi.json`; a client generating types from this
# contract stops being able to name them. Nothing that worked stops working,
# because none of the five did what it said.
#
# Sequencing note, in the spirit of the two this file already carries: this
# stacks straight on 4.0.0 (#1427) rather than amending it. `check_contract_version.py`
# refuses a structural change whose version stood still, and two contracts must
# never share a number. Stacking costs the clients nothing here, because NONE
# HAS ADOPTED 4.0.0: faultmaven-dashboard and faultmaven-copilot pin 3.8.0 and
# faultmaven-slack-agent pins 3.0.0, so each adoption PR crosses 4.0.0 and 5.0.0
# in the same move whether or not they are separate numbers. Amending 4.0.0
# would buy nothing and cost the record of which change was which.
#
# 4.0.0 — MAJOR. Three published filters that were never applied are REMOVED,
# not implemented. Batched into one bump because they are one defect, and a
# client should adopt the answer once.
#
#   * `include_archived` on `GET /api/v1/cases` (#1413). The `Query` parameter
#     and the argument it fed to `CaseListFilter`, which declares no such field
#     and sets no `model_config` — so Pydantic's default `extra='ignore'` had
#     been dropping it without a word, and no repository carried the predicate
#     either.
#   * `CaseSearchRequest.user_id` and `CaseSearchRequest.organization_id`
#     (#1416). Both published, both read by nothing.
#
# WHY REMOVE RATHER THAN IMPLEMENT, one at a time.
#
# `include_archived` has nothing behind it. There is no storage representation
# of an archived case: the `cases` table has no archived column, `alembic/`
# contains no archive migration, and
# `postgresql_hybrid_case_repository._row_to_case` states outright that
# `is_archived` / `archived_at` are gone. Implementing it is not a bug fix, it
# is the archival epic — see below, which is where that intent is now recorded
# in full.
#
# `user_id` is worse than dead. `search_cases(self, search_request,
# user_id=None)` already receives the AUTHENTICATED caller from the route, so
# `search_request.user_id` is a SECOND, client-supplied user id beside it.
# Inert, it is merely a lie; honoured, it is a cross-tenant read. There is no
# version of this field that both works and is safe on a caller-scoped
# endpoint.
#
# `organization_id` contradicts ADR-017. The organization answers "who pays for
# these accounts" and is never a visibility predicate — the ENTERPRISE is the
# isolation tenant. So there is no correct WHERE clause to write for it, which
# makes "implement it" not a smaller version of the same decision but a
# different and wrong one.
#
# THE CLIENT-FIRST STEP WAS ALREADY SATISFIED, and this is how that was
# established rather than assumed. `docs/development/api-contract-changes.md`
# orders a REMOVE client-first — never remove something still being read — so
# all three client repositories were grepped:
#
#   * faultmaven-dashboard sends none of them. It has a standing GUARD TEST,
#     `src/lib/cases/api.test.ts` ("never sends include_archived on the list
#     endpoint"), and `src/lib/cases/api.ts` sends `{query, limit, team_id}` to
#     `/cases/search` under a comment explaining that it deliberately omits
#     `state` because the field was declared and not applied — pointing here.
#   * faultmaven-copilot sends none of them from production code: all three
#     `getUserCases` call sites pass only `limit`/`offset`. One nuance worth
#     stating, because it is what this MAJOR bump exists to surface — its
#     `CaseListFilters` type is DERIVED from the generated operations type, so
#     `include_archived` is expressible there and one test
#     (`src/test/api/services/case-service.test.ts`) passes it explicitly and
#     asserts it reaches the query string. Nothing on the wire changes for that
#     client, but its adoption PR has to delete that line, and the compiler will
#     say so. That is the derived type working exactly as intended.
#   * faultmaven-slack-agent never lists and never searches: `POST
#     /api/v1/cases` and `POST /api/v1/cases/{id}/turns`, plus auth and health.
#
# WHAT A REMOVED QUERY PARAMETER DOES AND DOES NOT BUY. `?include_archived=true`
# will still be ACCEPTED after this — FastAPI drops an unknown query parameter
# silently and there is no way to make it refuse one. Likewise an unknown key in
# a `CaseSearchRequest` body, which `extra='ignore'` discards. What changes is
# that the CONTRACT no longer promises anything about them: a client reading this
# document is no longer told a filter exists, which is the whole of the lie. The
# request validation is deliberately not tightened alongside — that is a separate
# decision with its own blast radius, and tightening only the body half would
# leave the two halves of one removal behaving differently.
#
# WHAT THE MODELS' DOCSTRINGS NOW SAY, and deliberately do not say. Both were
# nearly used to assert the invariant — "every field here reaches the repository
# query" — and neither says it.
#
# For `CaseListFilter` the claim is simply FALSE: it has two readers with
# different appetites. `list_user_cases` reads every field; `list_all_cases`,
# behind `GET /api/v1/admin/cases`, reads only `state`, `source`, `limit` and
# `offset` and says in its own docstring that it deliberately ignores
# `include_empty`. So the docstring states the invariant PER READER and names
# the guard's two rule tables.
#
# For `CaseSearchRequest` the claim is now TRUE — 3.9.0 landed first and `state`
# is applied — and it is still not made, for a reason that does not depend on
# merge order. This text is published verbatim as the schema's description, and
# a description is the one part of the contract nothing checks:
# `check_contract_version.py` strips prose before comparing, precisely because
# no client breaks on a reworded sentence. A blanket guarantee written there
# would go stale the first time someone adds a field and forgets to wire it,
# with every gate still green and the published contract asserting exactly the
# thing #1416 was. So the docstring records what was REMOVED and why — history,
# which cannot rot — and points at
# `tests/unit/modules/case/test_declared_filters_reach_the_query.py`, which
# classifies every field PER SURFACE and goes red on one that reaches no query.
# An assertion that moves when the code does, rather than a sentence that does
# not, which is the whole difference this version is about.
#
# TWO INTERNAL FIELDS GO WITH THEM, and cost no contract surface at all:
# `CaseListFilter.user_id` and `CaseListFilter.organization_id`. `CaseListFilter`
# is NOT in `docs/reference/api/openapi.json` — it is a service-layer model, not
# a request body — so no client can see either one. They are the same defect,
# found by the same sweep, and `grep -rn 'filters\.user_id\|filters\.organization_id'
# faultmaven/` returns nothing. The route stops passing
# `user_id=current_user.user_id` into the constructor: `list_user_cases` takes
# the principal as its own argument and reads only `state`, `source`, `team_id`,
# `limit`, `offset`, `include_empty` and the creation-date window off the filter,
# so the constructor argument was a second copy that changed no answer.
#
# WHAT ARCHIVING WAS, carried forward here because deleting the parameter deletes
# one of the three places it survived (the other two: a note in the case-service
# tests, and a design document, both named below). Archiving was a first-class case state
# with `is_archived` / `archived_at` columns; it was DROPPED in the schema
# redesign (commit `7b5a1b93`), and the note left in
# `tests/unit/modules/case/domain/services/test_case_service.py` beside the
# removed `test_excludes_archived_cases_by_default` records the intent verbatim:
# it "will be reintroduced as a deliberate epic with retention policy, scheduled
# archival, and list-view filter UI". That is still the plan and this entry does
# not cancel it. `closed_at` and the terminal states are the nearest thing today
# and they are NOT the same concept — a case you have finished with is not a case
# you have put away. The parameter comes back when there is something behind it,
# as part of that epic, with a storage representation, a retention policy and a
# migration; until then an affordance that does nothing is worse than no
# affordance, and faultmaven-dashboard#51 is the evidence.
#
# ONE SECURITY PROBE CHANGES SHAPE, and it is worth saying why that is not a
# loss. `tests/integration/security/test_two_enterprise_surface_probe.py` injected
# `user_id` and `organization_id` into a `POST /cases/search` body to prove a
# request-supplied principal never widens the caller's scope. With the fields
# gone, that injection sends keys pydantic drops before any handler sees them —
# which the probe's own guard, `test_the_search_injection_names_only_real_request
# _fields`, correctly FAILS on: an injection naming an undeclared field measures
# the parser, not the boundary. (It failed in CI, on the `postgres` lane, which
# is exactly where a probe like that earns its keep.) So the injection now names
# only `team_id`, and the boundary those two stood for is closed BY CONSTRUCTION
# rather than by behaviour — the stronger of the two. The behavioural claim moves
# to the unit tier, and the guard's BACKWARD half still fails the moment a new
# tenant-shaped selector appears on the model without someone deciding whether it
# is attackable.
#
# The third trace is the one that was actively wrong, and it is corrected in the
# same change: `docs/architecture/specifications/llm-configuration-design.md`
# described the reverted Phase-1 archival implementation in the present tense,
# with `include_archived` and four other components marked "Done". It now says
# what is true, and points here. The design text is kept — it is still the plan
# — but a specification that describes a reverted feature as shipped is the same
# lie this entry is about, one document over.
#
# MAJOR because published surface DISAPPEARS. A request body is validated
# against a narrower schema and a query parameter leaves the document, so a
# client generating types from this contract stops being able to name them —
# which is the point: a name that cannot be typed cannot be sent in the belief
# that it does something. Nothing that worked stops working, because none of
# the three ever worked.
#
# Sequencing note, because this file has been bitten twice and says so: 3.9.0
# (#1416's ADD) merged first, as #1426, and this is rebased on top of it. The
# two entries sit side by side rather than colliding, which is what the ordering
# was for — 3.9.0 makes `state` work, 4.0.0 removes the three fields that never
# could.
#
# 3.9.0 — MINOR. `POST /api/v1/cases/search` applies the `state` it has always
# declared. `CaseSearchRequest.state` was published in this document, accepted
# by Pydantic, and read by nothing: `CaseService.search_cases` called
# `repository.search(query=, user_id=, limit=, shared_case_ids=,
# restrict_case_ids=)` and looked at no other field, and `ICaseRepository.search`
# declared no `state` parameter for it to reach. So `{"query": "db", "state":
# "resolved"}` answered **200 with resolved and unresolved cases alike** — no
# error, no warning, a highlighted filter chip over an unfiltered list (#1416).
#
# DECLARING A FIELD IS NOT APPLYING IT, and this is the third recorded instance
# of the same defect: faultmaven-dashboard#51 (a date picker that did nothing
# for months and was eventually deleted as a lie), #1413 (`include_archived` on
# `GET /cases`), and this one. It came within a review of being the fourth:
# faultmaven-dashboard#154 proposed sending `state` during a text search
# BECAUSE THE SCHEMA HAD IT, which would have recreated #51 inside the pull
# request that was closing it. The schema was checked; the service was not.
#
# The predicate lives in the SAME WHERE CLAUSE as the text match and the
# visibility scope. That is the rule #1409 established for the creation-date
# bounds, and it is not a matter of taste here: search applies its `limit` in
# SQL, so a state narrowed afterwards would be narrowing an already-limited
# page — and would answer "no matching cases" whenever the limit happened to be
# filled by rows in other states. Note what is NOT available as a cross-check on
# this surface: search publishes no total (`CaseSearchResponse` is unused; the
# route is `response_model=List[CaseSummary]`), so there is no count for a
# misplaced predicate to visibly disagree with. It is the one filter surface
# where the wrong placement would have stayed silent, which is why the tests
# assert the placement directly, on every repository that can be stood up
# (in-memory, SQLite, and PostgreSQL under the `postgres` marker).
#
# TWO THINGS SHIP BESIDE THE PREDICATE, both of them the same defect one slot
# over. `SessionlessCaseRepository` — the wrapper actually wired in at runtime —
# forwarded to `list` and `search` POSITIONALLY; inserting `state` ahead of
# `limit` is precisely the edit that makes a positional forward bind the wrong
# values, and the #405 rename already shipped that failure once (`list(state=…)`
# against a wrapper still declaring `status`, swallowed into "you have no
# cases"). Both forwards are now by keyword. And `search`'s second return value,
# documented as `(cases, total_count)` since the interface was written, was
# `len(cases)` in three of the four implementations — the page length wearing
# the name of a total. Nothing reads it (`search_cases` discards it and the
# route is `response_model=List[CaseSummary]`), which is exactly why it could
# stay wrong: a DECLARED value that is not the value declared. It is now a true
# COUNT over the same WHERE clause, as `list` has always computed it. Neither is
# a contract change — no published shape moves — but both are the shape of thing
# this version exists to stop.
#
# `state` is the only one of `CaseSearchRequest`'s three inert fields worth
# having. `user_id` and `organization_id` are NOT being implemented: on a
# caller-scoped endpoint the first is a second, client-supplied user id beside
# the authenticated one, and the second contradicts ADR-017, where the
# organization BILLS and is never a visibility predicate. Both are removed in
# 4.0.0, together with `include_archived`.
#
# MINOR, AND THE ARGUMENT FOR MAJOR WAS CONSIDERED. It has to be, because this
# entry itself quotes the rule that points the other way:
# `docs/development/api-contract-changes.md` says "a field that keeps its type
# and changes its meaning breaks clients and no tool will say so", and that is
# exactly what applying a previously-ignored field does. The MAJOR case is
# sharper still than for a removal: a REMOVAL reaches a client only when it
# moves the ref it pins, whereas this reaches every deployed client the moment
# the server rolls, with no version for them to gate on. If any client were
# sending `state` and rendering the unfiltered list it got back, this change
# would silently alter what its users see.
#
# It does not bite here, and that is VERIFIED rather than assumed: **no client
# sends the field.** `POST /cases/search` has exactly one caller in the field,
# faultmaven-dashboard, and it sends `query`, `limit` and `team_id` only — it
# deliberately WITHHOLDS `state` behind a comment pointing at #1416
# (`src/lib/cases/api.ts`), and disables its state chips during a text search
# rather than send something that gets dropped. faultmaven-copilot and
# faultmaven-slack-agent never call the route at all: the copilot's three
# `getUserCases` call sites are the list endpoint, and the slack agent only
# creates cases and submits turns. So the population whose behaviour could
# change is empty, and what is left is a surface that grew a working filter.
#
# On the mechanical side the published surface does not change shape either —
# nothing removed, nothing narrowed, nothing made required — so no client can
# fail to parse, or fail to send what it sent yesterday. Adoption on the
# dashboard side is the REMOVAL of a workaround.
#
# WORTH KNOWING FOR NEXT TIME: `check_contract_version.py` reports this as "no
# structural change (a re-publication)", and it is right — the only thing in the
# generated diff besides `info.version` is one reworded description. The version
# moves anyway, for the reason above: what changed is the MEANING of a field
# that was already published, and no differ can see that. This is what the
# tooling deliberately does not decide. A version that stood still here would
# tell a client nothing had happened on the one endpoint whose answers just
# changed — and the next person to weigh MINOR against MAJOR on a
# meaning-change would find no record that the question was asked.
#
# 3.8.0 — MINOR. `GET /api/v1/cases` accepts `created_after` and
# `created_before`. `CaseListFilter` has carried both fields since it was
# written, and the route never bound them as query params — so a client that
# sent a date got no error and no filtering: FastAPI drops an unknown query
# param silently, and the dashboard shipped a date picker that did nothing for
# long enough that it was eventually deleted as a lie
# (faultmaven-dashboard#51). This binds them.
#
# THE WINDOW IS HALF-OPEN: `[created_after, created_before)`, inclusive lower
# and EXCLUSIVE upper. An inclusive upper bound is not expressible by a client
# whose clock stops at milliseconds — which is every browser, because
# `Date.prototype.toISOString` does — while `created_at` keeps microseconds. A
# day bounded at 23:59:59.999 silently drops a case created at 23:59:59.9997,
# which is the same class of quiet row-loss this entry exists to remove. To
# select a calendar day, send that day's first instant and the FOLLOWING day's.
#
# THEY ARE INSTANTS, NOT CALENDAR DAYS. The server cannot know which day
# "2026-09-14" meant, so it does not guess: the client resolves the day in ITS
# OWN timezone. But any offset is NORMALIZED TO UTC before it reaches the query,
# and that is load-bearing rather than tidy — on SQLite `created_at` is stored
# as adapter-rendered TEXT and compared lexicographically, so
# `2026-09-11T00:00:00+05:30` and the identical instant written
# `2026-09-10T18:30:00Z` returned DIFFERENT rows until the bound was converted.
# A naive value is read as UTC, the only reading that does not shift by whatever
# zone the server happens to run in.
#
# An INVERTED window (`created_after > created_before`) is a 422, not an empty
# list: unsatisfiable by construction, it would otherwise answer "you have no
# cases in that range" when the truth is "you swapped the ends".
#
# Both predicates live in the same WHERE clause as every other filter, so
# `total_count` counts the same set the page comes from — the pagination rule
# `include_empty` already follows, and for the same reason: a bound applied
# after the repository paginated would thin an already-sliced page.
#
# Additive: every existing call is unaffected, both params default to None.
#
# 3.7.0 — MINOR. The investigation turn reaches the EVIDENCE surfaces. 3.5.0
# gave every schema that publishes a turn for display a nullable
# `investigation_turn`, and missed the two that name a turn they do not
# themselves render: evidence rows ("collected at turn N") and uploaded files
# ("uploaded at turn N"). Both carried only the message clock, so on any case
# with an aside a client printed `turn 5` beside an evidence row while its own
# transcript called that same exchange `Turn 4` — the defect #1387 described for
# history, one surface over (#1391).
#
# `SourceFileReference`, `EvidenceDetailsResponse`, `DerivedEvidenceSummary`,
# `UploadedFileMetadata` and `UploadedFileDetailsResponse` each gain a nullable
# `investigation_turn`: the per-row ORDINAL for the turn the row was collected
# or uploaded at, from `Case.investigation_turn_at` — the same function
# `Message.investigation_turn` uses, so the row and the exchange it belongs to
# cannot disagree.
#
# WHY THE SERVER AND NOT THE CLIENT. A client can only resolve an evidence row's
# ordinal by finding the conversation row on the same clock turn, which means
# holding the conversation. The Dashboard's evidence tab holds none — it is a
# separate fetch on a tab that never loads the transcript — and the copilot's
# store is capped to a recent suffix, so a file uploaded early in a long case
# has no local row either. Both clients would have had to render nothing.
#
# `collected_at_turn` and `uploaded_at_turn` are unchanged and still the message
# clock: they are what anchors and jump-to-turn are keyed on. ADDRESS a turn
# with those, DISPLAY the new one.
#
# Numbered 3.7.0, not 3.6.0, for the reason 3.6.0 itself records: #1388 took
# 3.6.0 while this sat in review and merged first. Two different contracts must
# never share a version, so this moves rather than collides and both entries
# stay. They describe unrelated surfaces.
#
# 3.5.0 — MINOR. The investigation turn reaches the surfaces that DISPLAY a
# turn. 2.7.0 put `investigation_turn` on `TurnResponse` (#1329) — the reply to
# a submitted turn, and the one schema no history read and no header read can
# reach. Every other surface therefore went on printing the message clock, so
# an aside still moved the number the user was looking at, which is the symptom
# #1329 set out to remove. Every schema that publishes a turn for DISPLAY gains
# a nullable `investigation_turn` (#1387):
#
#   * `Message` — the per-row ORDINAL: the message clock at that row minus the
#     out-of-band turns at or before it. Note this is not the case-level total
#     moved onto the row. Attaching that total to every row would print the
#     same number on all of them, because a count of "the turns so far" is only
#     the label of the newest row; the ordinal is what labels the other rows,
#     and the two agree exactly where they should, on the newest one.
#
#     Null on a `system` row. Those are background-job notices (runbook
#     conversion) stamped with whichever turn happened to be OPEN when the job
#     finished, so a number on one asserts membership in an exchange it had no
#     part in — a rule both clients already implement privately, and the kind
#     of duplication a server-computed field exists to remove.
#
#   * `CaseUIResponse_Inquiry`, `_Investigating`, `_Resolved`, and also
#     `CaseSummary` (`GET /cases`), `CaseDetail` (`GET /cases/{id}`) and
#     `AdminCaseMetadata` — the case-level COUNT, the same quantity
#     `TurnResponse.investigation_turn` reports, carried on the case reads so a
#     header, a resolution summary ("12 turns") or an exported archive can show
#     it without having just submitted a turn. All six, not the three the first
#     pass moved: a client reading the clock off whichever one it happens to
#     hold is how the same defect survives in a different corner.
#
# `turn_number` and `current_turn` are unchanged and still mean the message
# clock. That distinction is now load-bearing rather than incidental: the clock
# is what ADDRESSES a turn — conversation anchors, evidence `uploaded_at_turn`,
# suggestion liveness — and re-basing those to the displayed label would break
# jump-to-turn silently, with no error and no failing test. Display the
# ordinal, address by the clock.
#
# MINOR: nullable additions to response-only schemas. A client that ignores
# them renders exactly what it renders today, and one that reads them falls
# back to the clock when the field is absent. Adoption is tracked in
# faultmaven-copilot#251, faultmaven-dashboard#127 and
# faultmaven-slack-agent#64.
#
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
# 3.6.0 — MINOR. `POST /knowledge/documents` gains two optional body fields,
# `scope` and `team_id` (#1377). Uploading a finished runbook file was an
# operator privilege hard-wired to the global tier; it is now an input method
# like Convert and Write Runbook, and the operator gate moved to `scope ==
# "global"` where it belongs.
#
# MINOR rather than MAJOR, and the judgement is worth stating because the
# schema alone does not settle it. Two new OPTIONAL fields are textbook MINOR,
# but the DEFAULT BEHAVIOUR changed: a caller that sends no `scope` used to
# publish at global and now publishes at personal. That is a silent semantic
# change, which is usually the worse kind.
#
# It is MINOR because no existing client can observe it. The only product
# caller is faultmaven-dashboard, which adopts the change in the same batch
# (faultmaven-dashboard#142 / #145). faultmaven-copilot names the URL in one
# test and never calls it; faultmaven-slack-agent does not reference it. A
# client that DID rely on the old default would deserve MAJOR — if one appears
# before this is adopted, this entry is the thing to revisit.
#
# `scope` is also now a closed set (`personal | team | global`) rather than a
# free string, so the schema publishes an `enum`. That narrows what is accepted
# — but only to values the server ever handled: anything else reached an
# unguarded `else` branch that wrote into the global runbook tree and then 500'd.
#
#
# Numbered 3.6.0, not 3.5.0, and for the reason 3.4.0 records above: #1389 took
# 3.5.0 while this sat in review and merged first. Two different contracts must
# never share a version — a number that cannot tell two contracts apart is not
# doing its job — so this moves rather than collides, and both entries stay.
# They describe unrelated surfaces.
API_CONTRACT_VERSION = "5.0.0"
