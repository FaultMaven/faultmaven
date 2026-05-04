"""Background jobs for the agent module.

Currently provides ``storage_cleanup`` (TTL-based orphan-file sweep). See
``faultmaven.modules.agent.jobs.storage_cleanup`` for usage.

Note: an async evidence-retry job (``evidence_retry``) was previously
scaffolded here but removed — the underlying design (Scenario 2 in
``docs/architecture/data-processing/evidence-failure-modes.md``) was
deferred in favour of synchronous error codes + ``Retry-After`` headers.
The scaffolded ``evidence_turn_async_retry_*`` Prometheus metrics in
``infrastructure/observability/evidence_metrics.py`` remain registered
as a tripwire for if/when the design is revisited on telemetry signal.
"""
