"""Telemetry for the token revocation store: a counter (#1478) and a gauge (#1479).

The counter, on the request path, exists because the condition it measures used to be
invisible. ``AuthService._is_revoked`` caught every exception and returned
``False`` — a revoked token ACCEPTED, with a log line as the only signal — so
"how often can this deployment not read its revocation store?" was a question
only ``grep`` could answer, on logs that roll.

The check now refuses instead (the #1478 ruling), which makes the condition
loud. This makes it *measured*: the refusal rate is what tells an operator
whether a deployment is one lock contention away from an outage, and it is the
number the fail-open/fail-closed trade would have to be re-argued from if it
ever is.

``kind`` is the failing exception's class name — the same half of a
:class:`~faultmaven.config.revocation_storage.StorageFault` that
``GET /admin/config/status`` already publishes, and for the same reason: it
names the failure family (``OperationalError``, ``ConnectionError``,
``TimeoutError``) without the driver's message, which for SQLAlchemy carries
the full statement and its bound parameters. Cardinality is bounded by the
drivers' exception vocabulary, which is small and fixed.

Deliberately NOT labelled by user, token or route: this is a storage-health
signal, and the request identity is in the log line beside it.

The gauge, ``faultmaven_auth_revocation_watermarks``, is the keyspace
measurement the #1479 ruling asked for: how many per-user watermarks the store
holds that can still revoke something. On Redis that is every ``user:`` key; on
SQL it is the ``scope='user'`` rows not yet elapsed. It carries no labels, it is
refreshed by :class:`RevocationWatermarkCollector` off the request path and off
the scrape path, and no auth decision reads it. The store is deployment-wide, so
every replica reports the same value: aggregate with ``max()``, not ``sum()``.
"""

import asyncio
import logging
from typing import Any

from faultmaven.infrastructure.shims.metrics import Counter, Gauge

logger = logging.getLogger(__name__)

#: Seconds between watermark counts.
WATERMARK_REFRESH_INTERVAL_SECONDS = 300

#: Authenticated requests refused because the revocation store could not answer.
revocation_state_unknown_total = Counter(
    "faultmaven_auth_revocation_state_unknown_total",
    "Requests refused because the token revocation store could not be read, "
    "so whether the presented credential was revoked is unknown. Labelled by "
    "the failing exception's class name.",
    ["kind"],
)

#: Live per-user revocation watermarks in the deployment's store.
revocation_watermarks = Gauge(
    "faultmaven_auth_revocation_watermarks",
    "Per-user token revocation watermarks the store holds that can still "
    "revoke something. Deployment-wide: every replica reports the same value, "
    "so aggregate with max(), not sum().",
)


class RevocationWatermarkCollector:
    """Refreshes :data:`revocation_watermarks` from the store on an interval."""

    # ``store`` is an ``ITokenRevocationStore``; typed ``Any`` because this
    # package may not import the domain interface (import-linter).
    async def refresh(self, store: Any) -> None:
        """Count the live watermarks and publish the number."""
        revocation_watermarks.set(await store.count_user_watermarks())

    async def run_periodic(
        self,
        store: Any,
        interval: int = WATERMARK_REFRESH_INTERVAL_SECONDS,
    ) -> None:
        """Background loop: refresh first, then sleep ``interval`` seconds.

        A failed refresh sets the gauge to ``NaN``, so it reads as unknown and
        never as a stale count, and keeps the loop. Only the exception's class
        name is logged: a SQLAlchemy message carries the statement and its bound
        parameters.
        """
        while True:
            try:
                await self.refresh(store)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                revocation_watermarks.set(float("nan"))
                logger.warning(
                    "Revocation watermark count failed (non-fatal): %s",
                    type(exc).__name__,
                )
            await asyncio.sleep(interval)


collector = RevocationWatermarkCollector()
