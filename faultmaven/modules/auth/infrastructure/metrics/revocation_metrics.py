"""Request-path telemetry for the token revocation check (#1478).

One counter, and it exists because the condition it measures used to be
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
"""

from faultmaven.infrastructure.shims.metrics import Counter

#: Authenticated requests refused because the revocation store could not answer.
revocation_state_unknown_total = Counter(
    "faultmaven_auth_revocation_state_unknown_total",
    "Requests refused because the token revocation store could not be read, "
    "so whether the presented credential was revoked is unknown. Labelled by "
    "the failing exception's class name.",
    ["kind"],
)
