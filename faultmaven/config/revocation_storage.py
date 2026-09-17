"""Startup fail-fast gate: can the resolved revocation store actually answer?

``create_token_revocation_store`` decides WHICH store a deployment gets. This
asks the next question, which nothing else did: does the storage behind it
exist? For the Redis arm that is the client, already checked at boot
(``fakeredis_or_fail``, ``get_async_redis_client``'s ping). For the database arm
it is a table — and a missing table is not hypothetical, it is the default
outcome of one very ordinary operation.

Why a gate rather than a log line
---------------------------------
``token_revocations`` is created by the single ``001_enterprise_baseline``
migration, edited in place as this repository's campaign rule requires. A
standalone deployment that **upgrades its image instead of wiping** is already
stamped ``a1e0c17bd001``, so ``alembic upgrade head`` is a NO-OP and the table
never appears. Every previous in-place edit failed loudly there — a missing
``team_invitations`` breaks team invitations, visibly. This one does not:

* ``AuthService._is_revoked`` catches every exception and returns ``False``, so
  a revoked token is ACCEPTED and the only signal is a log line;
* the write paths (logout, OAuth ``/revoke``, admin revoke-tokens) raise, so
  they 500 — which reports a broken feature, not a disabled security control.

A security control that fails open must not be discoverable only from
``kubectl logs``, which is where a startup warning goes to die. So this refuses
to boot, beside the deployment-coherence, credential and investigation-tooling
gates, and for the same reason they exist.

The remediation is deliberately explicit, because the obvious move does not
work: ``alembic upgrade head`` reports success and changes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from faultmaven.config.deployment_coherence import DeploymentCoherenceError

logger = logging.getLogger(__name__)

#: A jti no token can carry, so the probe reads a real index and writes nothing.
_PROBE_JTI = "__faultmaven_startup_probe__"

#: What actually fixes an absent table, per backend. Deliberately NOT
#: ``fm-wipe-deployment --wipe``: that command's own docstring is headed "What it
#: does NOT do: the database" — it covers vectors, object storage and Redis, so
#: an operator following it sees success, restarts, and hits this refusal again
#: (#828 delta review). Worse, ``--verify`` asserts ``token_revocations`` is
#: empty and errors on the missing table.
REMEDIATION = (
    "   SQLite: stop the API, delete data/faultmaven.db (plus -wal/-shm), and "
    "restart — the baseline is re-applied to the new file on boot.\n"
    "   PostgreSQL: DROP DATABASE + CREATE DATABASE with the owner DSN, then run "
    "the migration.\n"
    "   Either way this DESTROYS the deployment's data, which is the campaign's "
    "documented upgrade path (pre-user: no backward compatibility, no data "
    "preservation)."
)


@dataclass(frozen=True)
class StorageFault:
    """A storage failure, split by who may see which half.

    ``kind`` is the exception class name — safe to publish. ``detail`` includes
    the driver's message, which for SQLAlchemy carries the full statement and its
    bound parameters; that goes to the operator's console and the server log, and
    never into an API response body.
    """

    kind: str
    detail: str


class RevocationStorageUnavailableError(DeploymentCoherenceError):
    """The resolved revocation store cannot reach its storage.

    A :class:`DeploymentCoherenceError` because it is the same statement those
    make: the running deployment contradicts what its configuration promises —
    here, that revocations are recorded somewhere.
    """


async def probe_revocation_storage(store) -> StorageFault | None:
    """Why this store cannot answer a revocation question, or None if it can.

    One read against the store's own API — not a hand-written ``SELECT``, so the
    probe exercises the same path the request path does and cannot pass while
    that path fails. Reads, never writes: a gate that wrote would leave a row
    behind on every boot.

    Returns a :class:`StorageFault` rather than a string, because its two
    callers need different halves of it. The boot gate prints ``detail`` to an
    operator's console; ``GET /admin/config/status`` publishes only ``kind``,
    since the driver message carries the full SQL and its bound parameters and
    an API response body is not where that belongs.

    Scoped to the database arm. The Redis arm's backing is its client, and that
    is proven at composition time (``fakeredis_or_fail`` refuses cloud without a
    real one; ``get_async_redis_client`` pings). An unrecognised store is left
    alone for the same reason the operator preflight leaves one alone: a future
    implementation must not become an outage in a gate that cannot understand it.
    """
    from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
        SqlTokenRevocationStore,
    )

    if not isinstance(store, SqlTokenRevocationStore):
        return None

    try:
        await store.is_revoked(_PROBE_JTI)
    except Exception as exc:  # noqa: BLE001 - any failure to read is the finding
        logger.error(
            "Token revocation storage is unreadable: %s: %s",
            type(exc).__name__,
            exc,
        )
        return StorageFault(
            kind=type(exc).__name__, detail=f"{type(exc).__name__}: {exc}"
        )
    return None


async def validate_revocation_storage(store) -> None:
    """Refuse to boot when the resolved revocation store cannot answer.

    Args:
        store: The store the composition root resolved.

    Raises:
        RevocationStorageUnavailableError: It cannot read its own storage.
    """
    if store is None:
        # Absence is already fatal at the composition root, which says so with
        # the context this function does not have. Not this gate's finding.
        return

    fault = await probe_revocation_storage(store)
    if fault is None:
        logger.info("✅ Token revocation storage verified (%s)", type(store).__name__)
        return

    raise RevocationStorageUnavailableError(
        "The token revocation store cannot read its storage, so revocation "
        f"would be unenforceable: {fault.detail}\n"
        "   Refusing to start: AuthService._is_revoked fails OPEN, so every "
        "revoked token would be accepted and the only signal would be a log "
        "line.\n"
        "   If this deployment was UPGRADED rather than re-provisioned, the "
        "token_revocations table (#828) is missing and `alembic upgrade head` "
        "will NOT create it — there is one migration and this database is "
        "already stamped with it.\n" + REMEDIATION
    )
