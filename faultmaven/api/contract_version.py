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
API_CONTRACT_VERSION = "2.2.0"
