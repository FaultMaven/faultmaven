"""LLM usage retention job — prune the usage ledger past its horizons (#640, Q6).

Usage:
    python -m faultmaven.jobs.run llm_usage_retention

Deletes ``llm_usage_daily`` rows whose UTC ``usage_date`` is older than today
minus ``LLM_USAGE_DAILY_RETENTION_DAYS`` (default 400), and ``llm_turn_spend``
rows whose ``occurred_at`` is older than now minus
``LLM_USAGE_TURN_RETENTION_DAYS`` (default 90). Nothing else: a row exactly at
its horizon stays.

Tenant scope: **cross_tenant**. The horizon is a deployment policy, not a
tenant's, and under the multi-tenant provider RLS would show an app-role run
one enterprise's rows. So the runner refuses it there except on the audited
maintenance path (``--cross-tenant-maintenance`` + the BYPASSRLS maintenance
role, which needs ``DELETE`` on both tables). In standalone it also runs under
the in-process scheduler when ``RUN_SCHEDULER=true``
(``infrastructure/tasks/llm_usage_retention``).
"""

import logging
from typing import Any, Dict

from faultmaven.config.settings import FaultMavenSettings

logger = logging.getLogger(__name__)


async def run(
    settings: FaultMavenSettings, container: Any, **kwargs: Any
) -> Dict[str, Any]:
    """Prune the usage ledger. Returns the runner's result shape.

    Args:
        settings: FaultMaven settings; the two horizons are read from it.
        container: DI container (unused — the ledger is reached directly).
        **kwargs: Runner-global parameters (unused).

    Returns:
        ``status`` ("completed" or "failed"), the horizons applied, and
        ``daily_rows_deleted`` / ``turn_rows_deleted``.
    """
    from faultmaven.infrastructure.llm.usage_ledger import prune_expired_usage

    daily_days = settings.auth.llm_usage_daily_retention_days
    turn_days = settings.auth.llm_usage_turn_retention_days
    result: Dict[str, Any] = {
        "job": JOB_NAME,
        "status": "completed",
        "daily_retention_days": daily_days,
        "turn_retention_days": turn_days,
        "daily_rows_deleted": 0,
        "turn_rows_deleted": 0,
    }
    try:
        pruned = await prune_expired_usage(
            daily_retention_days=daily_days, turn_retention_days=turn_days
        )
        result["daily_rows_deleted"] = pruned.daily_rows_deleted
        result["turn_rows_deleted"] = pruned.turn_rows_deleted
        logger.info(
            "LLM usage retention: deleted %d daily and %d turn row(s) "
            "(horizons %d / %d days)",
            pruned.daily_rows_deleted,
            pruned.turn_rows_deleted,
            daily_days,
            turn_days,
        )
    except Exception as e:
        logger.error(f"LLM usage retention job failed: {e}", exc_info=True)
        result["status"] = "failed"
        result["error"] = str(e)
    return result


# Job metadata for CLI discovery
JOB_NAME = "llm_usage_retention"
JOB_DESCRIPTION = "Prune LLM usage ledger rows past their retention horizons"
# The horizon applies to every enterprise's rows; under multi only the
# maintenance role sees them all, so the runner refuses an app-role run.
JOB_TENANT_SCOPE = "cross_tenant"
