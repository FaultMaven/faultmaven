# Logging Policy (Modular Monolith)

## Default levels
- Production: INFO
- Non-prod: INFO with optional DEBUG via FAULTMAVEN_DEBUG=1

## Component guidance
- Decision records: INFO (structured, sampled if needed; no PII)
- Gateway: DEBUG (clarity), WARN (absurd), ERROR (exceptions)
- Router: DEBUG (selection and scores), WARN (circuit/backoff), ERROR (selection failure)
- Skills: DEBUG (start/finish, budget skips), WARN (partial results), ERROR (exceptions)
- Confidence: DEBUG (final score/band), avoid full feature vectors in prod
- LoopGuard: DEBUG (signals), WARN (recovery), INFO (escalation)
- Retrieval: DEBUG (latency, count), WARN (adapter timeout), ERROR (adapter failure)
- Policy: INFO (confirmation required), WARN (deny), ERROR (engine error)
- Container/DI: INFO (wiring), WARN (degraded), ERROR (init failures)

## Sampling
- Decision records: target 5–10% sampling if volume high; otherwise 100% during hardening

## Structure
- JSON logs with fields: timestamp, level, component, session_id, case_id, event, payload

## Redaction
- Strip or hash PII/session identifiers; avoid storing raw user content in logs


