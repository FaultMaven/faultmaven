# Redaction / PII-Secret Sanitization — Design

Status: current. Tracks the hardening in issue #654.

FaultMaven redacts secrets and PII from data before it leaves the process
(outbound LLM calls, telemetry, stored evidence). This document states how the
subsystem is built and the invariants it must hold. Redaction is **off by
default** (`protection_enabled=False` / `sanitize_pii=False`) — the standalone
build runs without it; it is enabled in the cloud deployment where Presidio is
available.

## One sanitizer

`DataSanitizer` (`infrastructure/security/redaction.py`) is the single
implementation: ~regex patterns for secrets/keys/private-IPs/MACs, plus the
Presidio analyzer (a K8s microservice) for cards/emails/SSNs. `CaseRedactionContext`
(`infrastructure/security/case_redaction.py`) wraps it with a Redis-backed,
case-scoped registry so the same value maps to the same placeholder across a
case's files and turns, and can be reversed.

There is exactly one sanitizer path. (The former `PIIRedactor` shim and
`EnhancedSecurityAssessment` were unused and were removed.)

## Invariant 1 — Opaque model artifacts are never redacted

Provider reasoning artifacts — Gemini `thoughtSignature`, per-tool-call
`provider_metadata`, `assistant_parts`, and equivalents — are opaque base64
tokens generated *by* a provider and sent *back* to the same provider verbatim.
They contain no user data, so redacting them serves no privacy purpose and
**corrupts** them (a rewritten byte breaks the provider's base64 decode → the
model call fails). See the reasoning-artifact-passthrough rule.

Two enforcement layers, both required:

1. **No context-free entropy patterns.** A secret pattern must be anchored to
   context (e.g. `aws_secret_access_key = …`), never "any 40-char base64 run" —
   the latter matches every hash, SHA, and base64 blob, which is what corrupted
   a live `thoughtSignature`.
2. **Structural key exclusion.** The recursive sanitizer passes through, without
   walking, dict keys that carry opaque artifacts (`provider_metadata`,
   `thoughtSignature`, `thought_signature`, `assistant_parts`). This makes
   corruption impossible regardless of pattern breadth.

The LLM router sanitizes message **content** for both the outbound copy and the
telemetry copy; provider metadata rides through untouched by the exclusion above.

## Invariant 2 — Redaction never blocks the event loop

`DataSanitizer` does CPU-bound regex over large text plus a blocking HTTP
round-trip to the Presidio analyzer. On the async request path it MUST run off
the event loop via the async boundary (`CaseRedactionContext.asanitize` /
`DataSanitizer.asanitize`, which offload to a worker thread). A synchronous
`sanitize()` on an `async` path stalls the loop and, on Kubernetes, starves the
liveness probe — the same failure class as inline embedding. Presidio calls do
not add blocking retry sleeps on the loop.

## Invariant 3 — Fail closed on a runtime Presidio failure

If the Presidio analyzer was available and then **errors or times out during a
call**, the default is **fail-closed**: the un-analyzed text is not passed
through as if clean (a `RedactionUnavailableError` is raised).
`PROTECTION_FAIL_OPEN` (default `false`) controls this; operators may opt into
fail-open.

This is scoped to *runtime* failures, not to the analyzer never being
established. When Presidio is **not configured, its health check is skipped
(tests/dev), or it is down at startup**, `analyzer_available` is False and the
sanitizer runs **regex-only without raising** — otherwise every call would fail
whenever Presidio simply isn't wired up. A transient runtime error does **not**
latch `analyzer_available` off; the `BaseExternalClient` circuit breaker backs
off and recovers, so a momentary blip self-heals rather than permanently failing
every call.

Regex patterns always apply regardless (they are local and cannot fail-open).
Presidio is gated on the **analyzer** only (the anonymizer service is not used —
replacement is done in-process for stable placeholders).

## Placeholders

Detected values in **text** map to a stable, reversible pseudonym
`<TYPE_hash>` within their scope (the case registry gives cross-file
consistency; the hash is wide enough that distinct values don't collide onto one
placeholder). Values of **dict keys that name a secret** (`password`, `api_key`,
`auth_token`, …) are fully redacted to a fixed `[TYPE_REDACTED]` marker instead
— the value is discarded, not pseudonymized, because it should never be
recoverable. Secret-key names are matched as whole segments, so `monkey` /
`tokenizer` are not mistaken for secrets.
