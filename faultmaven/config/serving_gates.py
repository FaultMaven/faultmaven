"""The configuration gates a server process passes before anything is written.

Three refusals, in this order, each relying on the ones before it:

1. **Deployment coherence** (ADR-004): the running configuration matches
   ``DEPLOYMENT_MODE``. A cloud deployment must present cloud identity, never
   silently run as standalone.
2. **A persistent database** (fm#1647): an empty or in-memory
   ``DATABASE_URL`` used to boot as far as the enterprise seed and die there as
   "Critical bootstrap failure".
3. **Several server processes over what one process must hold** (#2269,
   #2281): a SQLite database, or a store the configuration alone makes
   process-local (``faultmaven/config/worker_stores.py``).

Two callers run them:

* the web lifespan, first thing, before ``resolve_pseudonym_key`` (which creates
  the data directory and writes the key file), so a refused boot leaves nothing
  on disk; and outside its test-environment skip, because no test boots the app
  without a database;
* ``run_server``, before uvicorn forks. Refused only in each worker's lifespan,
  a configuration the launcher could have refused starts the workers anyway,
  and uvicorn restarts a worker that exits during startup forever (#2281). The
  lifespan still runs them, for a launch that does not come through
  ``run_server``.

The CronJob runner (``faultmaven/jobs/run.py``) runs the first two: a job is one
process.
"""

from __future__ import annotations

from typing import Any

from faultmaven.config.deployment_coherence import validate_deployment_coherence
from faultmaven.config.persistent_database import require_persistent_database
from faultmaven.config.worker_stores import refuse_configured_per_process_stores


def refuse_unservable_configuration(settings: Any) -> None:
    """Run the three gates, in order.

    Raises:
        DeploymentCoherenceError: the configuration contradicts ``DEPLOYMENT_MODE``.
        NonPersistentDatabaseError: no persistent database is configured.
        PerProcessStoresError: several processes over SQLite or a process-local store.
    """
    validate_deployment_coherence(settings)
    require_persistent_database(settings)
    refuse_configured_per_process_stores(settings)
