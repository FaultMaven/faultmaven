"""In-process pruning of the LLM usage ledger, for standalone (#640, Q6).

The standalone counterpart of the ``llm_usage_retention`` job: when
``RUN_SCHEDULER=true`` the lifespan starts it beside the case-cleanup scheduler
and stops it on shutdown. It prunes once at start — a standalone install is
restarted more often than daily, and a first run a full interval later would
then never come — and every ``interval_hours`` after that.

``RUN_SCHEDULER`` is off by default, so a default standalone install does not
prune; the ledger grows by about 2 MB per 90 days at 100 turns a day, which the
operations doc states rather than engineers around.

Refused under the multi-tenant provider, like the case-cleanup scheduler: the
horizon is deployment-wide, and RLS would show a web worker one enterprise's
rows. Cloud prunes with the job, as a CronJob on the maintenance role.

An asyncio task on the app's own loop rather than an APScheduler thread: the
prune is one async DELETE per table through the application's engine, whose
pooled connections (PostgreSQL) belong to that loop.
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def prune_llm_usage_once() -> None:
    """One pass over both tables at the configured horizons. Never raises."""
    try:
        from faultmaven.config.settings import get_settings
        from faultmaven.infrastructure.llm.usage_ledger import prune_expired_usage

        observability = get_settings().observability
        pruned = await prune_expired_usage(
            daily_retention_days=observability.llm_usage_daily_retention_days,
            turn_retention_days=observability.llm_usage_turn_retention_days,
        )
        logger.info(
            "LLM usage retention: deleted %d daily and %d turn row(s)",
            pruned.daily_rows_deleted,
            pruned.turn_rows_deleted,
        )
    except Exception as e:
        logger.error(f"LLM usage retention pass failed: {e}", exc_info=True)


async def _prune_forever(interval_seconds: float) -> None:
    while True:
        await prune_llm_usage_once()
        await asyncio.sleep(interval_seconds)


def start_llm_usage_retention_scheduler(
    interval_hours: float = 24,
    is_multi_tenant: bool = False,
) -> Optional["asyncio.Task[None]"]:
    """Start pruning on the running loop; ``None`` when refused.

    Args:
        interval_hours: Hours between passes after the first (default 24).
        is_multi_tenant: Whether the deployment runs the multi-tenant provider,
            under which the scheduler is refused (see the module docstring).

    Returns:
        The task, for :func:`stop_llm_usage_retention_scheduler`.
    """
    if is_multi_tenant:
        logger.warning(
            "In-process LLM usage retention refused under the multi-tenant "
            "provider: the horizon is deployment-wide, but RLS scopes a web "
            "worker's reads to one enterprise. Run the llm_usage_retention job "
            "on the maintenance path instead."
        )
        return None
    task = asyncio.get_running_loop().create_task(
        _prune_forever(interval_hours * 3600.0)
    )
    logger.info(
        f"LLM usage retention scheduler started (interval: {interval_hours} hours)"
    )
    return task


async def stop_llm_usage_retention_scheduler(
    task: Optional["asyncio.Task[None]"],
) -> None:
    """Cancel the pruning task and wait for it to finish."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    logger.info("LLM usage retention scheduler stopped")
