# Owner decision brief — 2026-09-05 — the 86 documented logging variables that do not exist

Decisions that are **mine to raise and yours to make**, with the facts needed
to decide. Status key: ⬜ open · ✅ decided · ⚫ closed by events

`docs/operations/monitoring/` names **88 distinct `LOG_*` variables**. Exactly
**two** are settings fields: `LOG_LEVEL` and `LOG_FORMAT`. The other 86 are
silently accepted and discarded, because every settings class sets
`extra="ignore"`.

You asked me to filter the 86 for anything critically important rather than
delete them wholesale. This is that triage. One group turned out to matter, and
it is shipped. One question is genuinely yours.

Shipped from this triage: fm#1339 (credential leaks), fm#1341 (fifteen unread
agent/embedding knobs), fm#1342 (three logging knobs that configure nothing).
Filed: fm#1340 (redaction processor). Evidence added to fm#1335.

---

## The one group that mattered: redaction — implemented as behaviour, not as knobs

Nine of the 86 describe log redaction: `LOG_ENABLE_DATA_SANITIZATION`,
`LOG_SANITIZATION_MODE`, `LOG_SANITIZE_QUERY_PARAMS`, `LOG_SANITIZE_HEADERS`,
`LOG_SANITIZE_REQUEST_BODY`, `LOG_SENSITIVE_FIELDS`, `LOG_SENSITIVE_HEADERS`,
`LOG_SENSITIVE_PATTERNS`, `LOG_REDACTION_PLACEHOLDER`.

The capability they describe genuinely does not exist, and its absence was
costing us. Reading for it found **three live leaks**, all fixed in fm#1339:

| Sink | What reached the log store |
|---|---|
| `api/middleware/logging.py` stamped the whole query string at INFO | the SSO **authorization code** and CSRF state, on **every sign-in** |
| `httpx` logs the full request URL at INFO | the **Google API key** and the investigation's **search text**, on every web search (on by default) |
| `context_builder.py` logged `first_120_chars` | **Copilot-captured page content**, on a routine parse branch |

The second was measured end to end under the real logging configuration:

```
before:  HTTP Request: GET https://…/customsearch/v1?key=AIzaSy-REAL-GOOGLE-KEY-0000&cx=cx123&q=prod+outage%3A+db+creds+rotated+for+acme-corp&num=3 "HTTP/1.1 400 Bad Request"
after:   HTTP Request: GET https://…/customsearch/v1?<redacted> "HTTP/1.1 400 Bad Request"
```

Logs ship to Loki with 14-day retention, so each of these had a two-week
window.

**My call, which needs no decision from you: implement the behaviour,
implement none of the nine knobs.** A redaction control that an operator can
switch off is not a security control, and the specific knob that looks most
relevant would not have helped: `LOG_SANITIZE_QUERY_PARAMS=true` sanitises a
generic query-parameter channel, and two of the three leaks were in fields the
application assembled itself. What was missing was the behaviour, and behaviour
that matters should not be optional. fm#1340 files the central processor;
fm#1339 fixes the three known sinks at the source, where a value's meaning is
actually known.

---

## D1 ⬜ The `api` latency threshold fires on normal operation

**Facts.** `PerformanceTracker.thresholds` hardcodes `{"api": 0.1, "service":
0.5, "core": 0.3, "infrastructure": 1.0}`, and
`api/middleware/logging.py:174` records **every HTTP request's total duration**
against `layer="api"`. Exceeding it emits `WARNING  Slow request detected`.

Every investigation turn makes at least one LLM call and takes seconds. So
every turn — and every KB search, every upload — trips a 100 ms threshold. The
WARNING level in this deployment therefore fires on healthy traffic, which is
what makes it unusable as an alerting signal.

Four of the 86 variables would have made this configurable
(`LOG_PERF_THRESHOLD_API`, `…_SERVICE`, `…_CORE`, `…_INFRASTRUCTURE`).

**What I cannot determine.** What number is right. A constant like this needs a
measured anchor, and I do not have the distribution of real request durations —
that lives in the Prometheus histogram on the live deployment. Guessing 2 s
versus 5 s here would produce exactly the arbitrary constant we have now, with
a different value.

**Options.**

- **A. Delete the warning; alert from the histogram instead.** The same
  middleware already observes `http_request_duration_seconds` with per-endpoint
  labels, three lines above. A per-request log line is a strictly weaker
  instrument than a histogram: it cannot express a percentile, it cannot be
  aggregated, and it fires per event. Cost: an operator surface goes away, and
  anyone who greps for "Slow request detected" loses it.
- **B. Keep it, set the threshold from a measurement.** You read the histogram,
  pick a p99-ish number per layer, and I change the constants with the
  measurement recorded beside them. Cost: one number for all endpoints is still
  wrong in principle — `/health` and `POST /cases/{id}/turns` are not the same
  thing.
- **C. Make it route-aware.** Correct, and the most work. Needs a per-route
  budget table that then has to be maintained.

**My recommendation: A.** The histogram already exists, already has the labels,
and is where latency alerting belongs. The log line duplicates it worse. If you
would rather keep a log signal, B is fine but I need the measurement from you
first — I will not invent the number.

---

## The other 77: not applicable or low value, and here is why

Grouped by the reason, not by name. Every one of the 86 is in exactly one
group below or in the redaction group above; the counts were checked
programmatically against the names actually present in the documents.

**Log shipping and transport (24) — the collector owns this.**
`LOG_ELASTICSEARCH_*` (5), `LOG_SYSLOG_*` (4), `LOG_OUTPUT_FILE`,
`LOG_FILE_PATH`, `LOG_FILE_MAX_SIZE_MB`, `LOG_FILE_MAX_FILES`,
`LOG_FILE_ROTATION`, `LOG_DIR`, `LOG_OUTPUT`, `LOG_OUTPUT_STDOUT`,
`LOG_OUTPUT_STDERR`, `LOG_HANDLER`, `LOG_STDOUT_FORMAT`, `LOG_BUFFER_SIZE`,
`LOG_FLUSH_INTERVAL`, `LOG_RETRY_ATTEMPTS`, `LOG_REQUEST_TIMEOUT`.

The application writes JSON to stdout and a Grafana Alloy DaemonSet ships it to
Loki. Buffering, retrying, circuit-breaking and rotating are the collector's
job. Implementing them in-process would mean holding records in memory that we
lose on crash — precisely the records worth having. This is the group that
makes the document read as though it describes a different product.

**Duplicates of something that already works unconditionally (11).**
`LOG_ENABLE_TRACING`, `LOG_OTEL_TRACE_CORRELATION`, `LOG_OTEL_SPAN_EVENTS`,
`LOG_ENABLE_REQUEST_CONTEXT`, `LOG_ENABLE_METRICS`, `LOG_METRICS_ENABLED`,
`LOG_METRICS_ENDPOINT`, `LOG_METRICS_INTERVAL`, `LOG_METRICS_PREFIX`,
`LOG_CORRELATION_ID_HEADER`, `LOG_ENABLE_DEDUPLICATION`.

Trace and request context are injected by processors that always run. Metrics
are Prometheus, configured by `ENABLE_METRICS`, a different subsystem
altogether. Deduplication is real and works — it is `log_once`, keyed on a
per-request set — it simply is not, and should not be, switchable.

**Cosmetics with one correct answer (6).** `LOG_JSON_INDENT`,
`LOG_JSON_ENSURE_ASCII`, `LOG_ENABLE_COLORS`, `LOG_TIMESTAMP_FORMAT`,
`LOG_INCLUDE_CALLER_INFO`, `LOG_INCLUDE_STACK_TRACE`. Indenting JSON breaks line-oriented ingestion.
Colours are already decided by `isatty`. Timestamps are ISO-8601 because Loki
parses them.

**Bounds on a leak that does not exist (9).** `LOG_ENABLE_MEMORY_TRACKING`,
`LOG_ENABLE_LEAK_DETECTION`, `LOG_LEAK_DETECTION_THRESHOLD`,
`LOG_MEMORY_CHECK_INTERVAL`, `LOG_MAX_CONTEXT_MEMORY_MB`,
`LOG_MAX_CONTEXT_ATTRIBUTES`, `LOG_MAX_ATTRIBUTE_SIZE_BYTES`,
`LOG_MAX_LOGGED_OPERATIONS`, `LOG_MAX_PERFORMANCE_HISTORY`. The request context
is per-request and released at the end of the request; nothing accumulates
across requests. Bounding it would guard a hazard the design does not have.

**Health and self-monitoring of the logger (10).** `LOG_HEALTH_CHECK_*` (3),
`LOG_HEALTH_TIMEOUT`, `LOG_CIRCUIT_BREAKER`, `LOG_CIRCUIT_BREAKER_ENABLED`,
`LOG_EXTERNAL_SERVICES`, `LOG_EXTERNAL_SERVICE_MONITORING`,
`LOG_EXTERNAL_TIMEOUT`, `LOG_RATE_LIMIT_LOGGING`. These presuppose the logger
makes network calls it might fail at. It writes to stdout.

**Access-log and profiling shaping (17).** `LOG_ENABLE_ACCESS_LOGGING`,
`LOG_ACCESS_LOG_LEVEL`, `LOG_FAILED_ACCESS_LOG_LEVEL`,
`LOG_ENABLE_BOUNDARY_LOGGING`, `LOG_ENABLE_OPERATION_TRACKING`,
`LOG_ENABLE_OPERATION_PROFILING`, `LOG_ENABLE_PERFORMANCE_TRACKING`,
`LOG_PERFORMANCE_SAMPLE_RATE`, `LOG_PERFORMANCE_VIOLATION_LOG_LEVEL`,
`LOG_PROFILE_SLOW_OPERATIONS`, `LOG_SLOW_OPERATION_THRESHOLD`,
`LOG_SESSION_ID_HEADER`, `LOG_USER_ID_HEADER`, and the four
`LOG_PERF_THRESHOLD_*` covered by D1. Real knobs for a real subsystem, but each
one is a switch for behaviour that is currently unconditional and cheap. Adding
seventeen switches to turn off things nobody has asked to turn off is how the
first 86 were born.

---

## What was already inert on the other side of the line

Worth recording, because it reframes fm#1335. The document is not merely
missing 86 fields; it is also wrong about the ones that existed.

fm#1342 removes three settings that were declared and did nothing:
`LOG_DEDUPE` (its processor rebuilt a dict, whose keys are unique by
construction — and its test passed because the "duplicate" key in the test's
dict literal was collapsed by Python at parse time), and `LOG_BUFFER_SIZE` /
`LOG_FLUSH_INTERVAL` (bound, reported by `GET /health/logging`, read by no
buffering code — none exists).

The document's versions of those are wrong three ways over: wrong defaults
(`1000`/`1.0` against `100`/`5.0`), and for deduplication it names
`LOG_ENABLE_DEDUPLICATION`, which never existed, while the field that did exist
was undocumented and inert.

This bears on the open fm#1335 decision. Patching only the dangerous procedures
assumes the rest is broadly right. What is now measured is that even the
entries passing "does this exist?" fail on default, on name, and on whether the
knob does anything.
