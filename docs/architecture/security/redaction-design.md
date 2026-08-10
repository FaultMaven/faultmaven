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
replacement is done in-process so each value gets our own keyed pseudonym).

## Placeholders

Detected values in **text** map to a reversible pseudonym `<TYPE_digest>`,
where the digest is `HMAC-SHA256(deployment pseudonym key, value)` truncated to
64 bits.

The construction has to satisfy two requirements that pull against each other:

- **Unguessable from redacted output.** The spaces redaction protects run
  10^7–10^10 candidates — internal IPs, SSNs, phone numbers — so an *unkeyed*
  digest is a commitment to the plaintext: anyone holding the redacted text can
  enumerate the space offline and match digests. (Until #971 this was
  `md5(value)[:12]`, exactly that.)
- **The same for the same value across separately-sanitized artifacts.**
  Evidence files are redacted one at a time and persisted, the KB at ingestion,
  prompts per turn — each with its own registry. If one host reads as two
  placeholders across those, the investigation loses the co-reference it exists
  to find, and an LLM told about two hosts can conclude something false about
  either.

Only a keyed deterministic function satisfies both: a random per-registry token
gives the first and destroys the second, an unkeyed hash the reverse.

Truncation is 64 bits, wider than the 48 of the digest it replaced. A collision
maps two distinct values onto one placeholder and the reverse map then resolves
one of them to the **wrong** original — a wrong value handed back, not merely a
lost one. The redaction registry is unbounded (`entity_registry_cap_per_type`
caps a different subsystem's extractor, not this one), so the size to plan for
is however many distinct entities a large log holds: over 10^5 entries the
birthday bound is ~2e-5 at 48 bits and ~3e-10 at 64.

Consequence to be explicit about: within a deployment the same value always
produces the same placeholder, so redacted artifacts *can* be correlated by
anyone who can already read them. That is the co-reference above, not a
separate leak — it cannot be removed without removing the correlation the
product depends on. Cross-tenant reads are stopped by RLS, not by the digest.

### The pseudonym key

Owned by `infrastructure/security/pseudonym_key.py` and deployment-wide, never
per-tenant: global KB content is shared across tenants, so a per-tenant key
would break co-reference between a shared runbook and a tenant's own case.

| Deployment | Source |
|---|---|
| Cloud | `REDACTION_PSEUDONYM_KEY`, supplied as a deployment secret. **Required** — startup refuses without it. |
| Standalone | `REDACTION_PSEUDONYM_KEY` if set, otherwise generated once and persisted at `REDACTION_PSEUDONYM_KEY_PATH` (default `./data/.redaction_pseudonym_key`, mode 0600). |

Cloud refuses to generate because replicas do not share a data volume: each pod
would mint its own key and silently produce a different placeholder for the
same host. The key is resolved at startup, so a missing one fails the boot
rather than scattered requests, and the standalone key file is never created by
a live request.

Every path fails closed, because a weak key is worse than a missing one — it
looks configured, and `HMAC-SHA256("", value)` is a fixed publicly-recomputable
function of the value, i.e. the original defect wearing a keyed API:

- Both sources are whitespace-stripped, so the same secret supplied as a file
  and as an env var (k8s Secrets and `base64 <<< "key"` append newlines) keys
  the same HMAC.
- A configured key shorter than 16 characters is refused. A blank or
  whitespace-only one counts as *unset* rather than as an empty key — taking it
  literally would give every deployment that exported it blank the same key.
- The generated file is published by writing a temp file, fsyncing, and
  `link()`-ing it into place. `O_CREAT|O_EXCL` alone serializes the create but
  not the create-and-write, so a second worker reading in that window gets zero
  bytes — and standalone supports `WORKERS>1`, whose processes race on first
  start. Where `link()` is unsupported (some network and bind mounts) the
  fallback takes an exclusive `flock`, **re-reads under it**, and lands the
  content with an atomic `replace`. Re-deciding under the lock is what makes
  racers converge: `replace` is unconditional, so a racer holding its own
  candidate would otherwise overwrite the winner.
- A key file that exists but is empty or too short is refused, not replaced.
  The reason is not concurrency — a lock makes replacement race-free, and the
  fallback above uses one. It is that a corrupt file means the previous key is
  *gone*, so any replacement disagrees with every placeholder already written
  into stored evidence, KB content and transcripts: the same host reads as one
  placeholder in old material and another from then on. No protocol can repair
  that, because the information needed no longer exists. The cost of refusing
  is real — a corrupt file stops the boot even with redaction switched off —
  and it is accepted because a wrong-conclusion risk outranks an availability
  loss.

### Agreement across processes

Resolving a key does not establish the invariant that matters: that *every*
process redacting for this deployment uses the same one. Whether a generated
key is shared is a property of the topology — one process, or several sharing a
durable filesystem, is fine; several pods with no shared volume is not — and
the application cannot see its own topology. It used to infer from
`DEPLOYMENT_MODE=cloud`, which is a proxy an operator can simply not set: the
on-prem cluster does not, so a multi-replica Deployment took the standalone
path and minted a key per pod, silently, with no crashloop and no failed
rollout.

So the invariant is checked rather than guessed at. At startup each process
publishes `sha256(key)` to `redaction:pseudonym_key_fingerprint` in Redis under
`SETNX` — the one store every replica genuinely shares — and any process that
finds a *different* fingerprint already there refuses to serve. This holds
whatever the deployment mode claims, however the key was obtained, and whatever
the topology turns out to be; it also catches a half-rolled-out change to the
secret, and pods started against different values of it.

Two properties worth stating:

- **A digest is published, never the key.** This same Redis holds the
  placeholder→plaintext registry, and the design turns on not putting the key
  beside it. A SHA-256 of a 256-bit random value discloses nothing.
- **An unreachable Redis degrades rather than blocks.** An unavailable check is
  not evidence of disagreement, and failing closed on it would make redaction
  depend on Redis uptime. Standalone's in-process FakeRedis makes this a
  self-check that always agrees — correct, since one process cannot disagree
  with itself, and workers sharing the key file converge anyway.

The cloud refusal above remains, but as an earlier and clearer error rather
than the guarantee; this check is the guarantee.

It is deliberately kept out of the stores holding redacted data or the mapping
back from it. `CaseRedactionContext` persists the placeholder→plaintext registry
in **Redis** (`redaction:{case_id}`), and the redacted artifacts themselves live
in the application database; a key in either would hand a single compromise both
halves.

Rotating the key renumbers every future placeholder; placeholders already
written into stored evidence, KB content and transcripts keep the digest they
were written with and stop reversing. Rotation is therefore a deliberate act,
not routine hygiene.

Values of **dict keys that name a secret** (`password`, `api_key`,
`auth_token`, …) are fully redacted to a fixed `[TYPE_REDACTED]` marker instead
— the value is discarded, not pseudonymized, because it should never be
recoverable. Secret-key names are matched as whole segments, so `monkey` /
`tokenizer` are not mistaken for secrets.
