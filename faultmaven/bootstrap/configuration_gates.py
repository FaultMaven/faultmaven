"""Every startup refusal the configuration alone decides, in one order (#2281).

Two callers run :func:`validate_configuration`:

* the web lifespan, first thing, before composition;
* ``run_server``, when uvicorn is to run several workers, before it forks.
  Refused only in each worker's lifespan, a configuration the launcher could
  have refused starts the workers anyway, and uvicorn's supervisor restarts a
  worker that exits during startup forever. With one process the launcher
  leaves it to the lifespan, whose refusal stops uvicorn.

What only a built client or a live server can tell (a Redis that does not
answer, an unreachable database) is refused later, in composition; with several
workers each one retries it, as it would retry a server that is not up yet.
"""

from __future__ import annotations

from typing import Any


def validate_configuration(settings: Any, *, advisories: bool = True) -> None:
    """Refuse a configuration no server process may run; each gate relies on
    the ones before it.

    ``advisories=False`` skips the warnings that refuse nothing, so a launcher
    that validates before the workers do does not print them twice.

    Each gate is imported where it runs, so a test that replaces one on its
    module replaces it here too.
    """
    from faultmaven.bootstrap.composition import _is_test_environment
    from faultmaven.config.deployment_coherence import validate_deployment_coherence
    from faultmaven.config.persistent_database import require_persistent_database
    from faultmaven.config.worker_stores import refuse_configured_per_process_stores
    from faultmaven.infrastructure.security.pseudonym_key import (
        resolve_pseudonym_key,
    )

    # The running config must match DEPLOYMENT_MODE (ADR-004): a cloud
    # deployment must present cloud identity, never silently run as standalone.
    validate_deployment_coherence(settings)

    # A persistent database (fm#1647). An empty or in-memory DATABASE_URL used
    # to boot as far as the enterprise seed and die there as "Critical
    # bootstrap failure". Before resolve_pseudonym_key, which creates the data
    # directory and writes the key file, so a refused boot leaves nothing on
    # disk; and outside the test-environment skip below, because no test boots
    # the app without a database.
    require_persistent_database(settings)

    # Several server processes over a SQLite database or a store the
    # configuration alone makes process-local (#2269, #2281): before anything
    # is written, migrated, seeded or opened. What only the built clients can
    # tell is refused inside composition (faultmaven/config/worker_stores.py).
    refuse_configured_per_process_stores(settings)

    # A pseudonym key redaction may use. Resolving is what creates the
    # standalone key file, so the first redaction of the process is never the
    # thing that writes it. In cloud an unset key raises: generating one per
    # pod would give each replica a different placeholder for the same value,
    # and discovering that mid-request -- the regex pass runs on every sanitize
    # call, whatever PROTECTION_SANITIZE_PII says -- would surface as scattered
    # 500s rather than a refusal to start (#971).
    resolve_pseudonym_key(settings)

    # Skipped in test environments (pytest / SKIP_SERVICE_CHECKS), which boot
    # the app without real credentials.
    if _is_test_environment(settings):
        return

    from faultmaven.config.investigation_capability import (
        validate_investigation_tooling,
        validate_structured_output_capacity,
        warn_best_effort_enforcement,
    )
    from faultmaven.config.llm_validation import validate_llm_provider_credentials
    from faultmaven.infrastructure.llm.providers.registry import get_registry

    # An LLM provider explicitly chosen, with its credential. There is no
    # default provider: a silent default would only fail later, mid-turn, with
    # an opaque error.
    validate_llm_provider_credentials(settings)

    # The resolved investigation model (DA -> CHAT) can call tools: the engine
    # needs them to gather evidence (search_file, deep_analysis), and
    # concluding without reaching the evidence is the premature-conclusion
    # failure we guarantee against. Explicit opt-out:
    # ALLOW_TOOLLESS_INVESTIGATION (degraded/offline).
    validate_investigation_tooling(settings, get_registry())

    # Second capability axis: can the resolved structured-output model SERVE
    # the engine's response schemas? A constrained-decoding backend can reject
    # the larger stage schemas outright, and it does so only once a case
    # reaches that stage -- so without this gate an incompatible model runs
    # several turns of a live investigation and then fails every remaining
    # turn. Fails open when capacity is unmeasured; no opt-out flag, because
    # there is no degraded mode that still records investigation state.
    validate_structured_output_capacity(settings, get_registry())

    # Third, ADVISORY axis: schema-enforcement class per resolved role. Warns
    # (never blocks) when investigation/chat resolve to a model whose response
    # schemas are only requested in-prompt (BEST_EFFORT) -- the degraded-state
    # failure is otherwise silent and discovered from broken investigations,
    # not from boot. Classifier/synthesis roles are exempt by design.
    if advisories:
        warn_best_effort_enforcement(settings, get_registry())
