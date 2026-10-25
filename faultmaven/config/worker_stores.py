"""Several server processes, and the stores a process would hold alone (#2269).

uvicorn runs ``WORKERS`` processes, and every one runs the lifespan and composes
a container of its own. That is sound for a store each process reaches over the
network and wrong for one held inside the process:

* the in-process **FakeRedis**: each process would keep its own sessions, token
  revocations, rate limits and idempotency claims;
* a local ChromaDB **PersistentClient**: every process would open the same
  directory, each with its own in-memory index.

So several processes are refused when either store is process-local, at the
earliest point that can know it:

1. **Before composition** (:func:`refuse_configured_per_process_stores`, from the
   lifespan), when the configuration alone decides it: ``SKIP_SERVICE_CHECKS``
   (no Redis server is even tried), no Redis server named, or no ChromaDB
   server selected. Nothing is migrated, seeded or opened first.
2. **Right after the container builds its clients**, before the data layer
   (:func:`refuse_built_per_process_stores`, from the composition root), for
   what only the built clients can tell: standalone falls back to FakeRedis
   when the named Redis does not answer (the default ``REDIS_HOST`` is a
   Kubernetes service name), and to a local ``PersistentClient`` when the
   ChromaDB server does not.
3. **When composition fails in a mode that permits a partial API**
   (:func:`refuse_partial_composition`): nothing then says which stores exist,
   and the request middleware builds a FakeRedis per process when no client
   was published.

Several processes over one **SQLite** database are refused too (#2281, owner
ruling 2026-10-25): SQLite is the one-box install and serves one process.
Several workers need PostgreSQL, and ``run_server`` then migrates once before
uvicorn forks, so the workers do not race on a first boot.

No storage-type setting decides any of this: ``SESSION_STORAGE_TYPE`` and
``CASE_STORAGE_TYPE`` selected nothing and are gone.

This module reads settings only; the composition root classifies the built
clients (``is_fakeredis``, ``chroma_client_kind``) and passes the answers in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, List, Sequence

from faultmaven.config.deployment_coherence import is_external_chroma_configured

#: What each process would hold alone.
FAKEREDIS = (
    "the in-process FakeRedis, which holds sessions, token revocations, rate "
    "limits and idempotency claims"
)
LOCAL_CHROMA = "a local ChromaDB PersistentClient on the same directory"

#: The settings that change what is built.
REDIS_REMEDY = "REDIS_URL (or REDIS_HOST) naming a Redis server every process reaches"
CHROMA_REMEDY = (
    "CHROMADB_URL naming a ChromaDB server (with VECTOR_STORAGE_TYPE=chromadb)"
)


class PerProcessStoresError(RuntimeError):
    """Several server processes would each hold a store the deployment shares.

    A ``RuntimeError``: ``compose_application`` re-raises that class in every
    deployment mode, so a refusal from inside composition is never tolerated
    as a partial API.
    """


@dataclass(frozen=True)
class PerProcessStore:
    """One store each server process would hold alone."""

    what: str
    cause: str
    remedy: str


def serving_processes(settings: Any) -> int:
    """How many processes uvicorn runs the app in.

    ``WORKERS``, except under ``RELOAD``: uvicorn's reloader serves one process
    and ignores ``workers`` ("workers" flag is ignored when reloading is
    enabled).
    """
    server = settings.server
    if getattr(server, "reload", False):
        return 1
    return int(server.workers)


def configured_per_process_stores(settings: Any) -> List[PerProcessStore]:
    """The stores the configuration alone makes process-local.

    The container's factories decide the same way: ``create_redis_client``
    goes straight to FakeRedis under ``SKIP_SERVICE_CHECKS``, and the Redis
    client factory when neither a URL nor a host is set; the ChromaDB factory
    builds a local ``PersistentClient`` unless
    :func:`is_external_chroma_configured` (and no client at all under
    ``SKIP_SERVICE_CHECKS``).
    """
    found: List[PerProcessStore] = []
    skip = bool(settings.server.skip_service_checks)
    db = settings.database
    if skip:
        found.append(
            PerProcessStore(
                FAKEREDIS,
                "SKIP_SERVICE_CHECKS=true builds no Redis connection",
                f"SKIP_SERVICE_CHECKS=false with {REDIS_REMEDY}",
            )
        )
    elif not (db.redis_url or db.redis_host):
        found.append(
            PerProcessStore(
                FAKEREDIS,
                "neither REDIS_URL nor REDIS_HOST names a server",
                REDIS_REMEDY,
            )
        )
    if not skip and not is_external_chroma_configured(settings):
        found.append(
            PerProcessStore(
                LOCAL_CHROMA,
                "CHROMADB_URL is unset or VECTOR_STORAGE_TYPE is not chromadb",
                CHROMA_REMEDY,
            )
        )
    return found


def built_per_process_stores(
    settings: Any, *, redis: str, local_chroma: Sequence[str]
) -> List[PerProcessStore]:
    """The stores the built clients make process-local.

    ``redis`` is ``"server"``, ``"fakeredis"`` or ``"missing"`` (no client was
    built); ``local_chroma`` names the ChromaDB clients (``"KB"``,
    ``"evidence"``) that are local ``PersistentClient``\\ s.
    """
    found: List[PerProcessStore] = []
    if redis == "fakeredis":
        found.append(
            PerProcessStore(
                FAKEREDIS,
                "the Redis server the configuration names did not answer, so "
                "the client fell back to FakeRedis",
                REDIS_REMEDY,
            )
        )
    elif redis == "missing":
        found.append(
            PerProcessStore(
                FAKEREDIS,
                "no Redis client was built, so each process's middleware "
                "builds a FakeRedis of its own",
                REDIS_REMEDY,
            )
        )
    if local_chroma:
        plural = len(local_chroma) > 1
        clients = " and ".join(local_chroma) + (" clients" if plural else " client")
        if is_external_chroma_configured(settings):
            cause = (
                "the ChromaDB server CHROMADB_URL names did not answer, so the "
                f"{clients} fell back to {'local ones' if plural else 'a local one'}"
            )
        else:
            cause = (
                "CHROMADB_URL is unset or VECTOR_STORAGE_TYPE is not chromadb, "
                f"so the {clients} {'are' if plural else 'is'} local"
            )
        found.append(PerProcessStore(LOCAL_CHROMA, cause, CHROMA_REMEDY))
    return found


def refuse_per_process_stores(settings: Any, found: Iterable[PerProcessStore]) -> None:
    """Refuse when several processes would serve and ``found`` is not empty.

    Raises:
        PerProcessStoresError: naming each store, why it is process-local, and
            the settings that clear it.
    """
    found = list(found)
    processes = serving_processes(settings)
    if processes <= 1 or not found:
        return
    stores = "; and ".join(f"{store.what} ({store.cause})" for store in found)
    remedies = " and ".join(store.remedy for store in found)
    raise PerProcessStoresError(
        f"WORKERS={processes} runs {processes} server processes, and each would "
        f"hold its own copy of {stores}. Set WORKERS=1, or set {remedies}."
    )


def refuse_sqlite_across_workers(settings: Any) -> None:
    """Refuse several server processes over one SQLite database (#2281).

    Raises:
        PerProcessStoresError: when several processes would serve and
            ``DATABASE_URL`` names SQLite.
    """
    processes = serving_processes(settings)
    if processes <= 1:
        return
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    try:
        backend = make_url(settings.database.database_url or "").get_backend_name()
    except ArgumentError:
        return  # not a URL at all: require_persistent_database refuses it
    if backend == "sqlite":
        raise PerProcessStoresError(
            f"WORKERS={processes} runs {processes} server processes over one "
            "SQLite database, the one-box install, which serves one process. "
            "Set WORKERS=1, or set DATABASE_URL to a PostgreSQL server."
        )


def refuse_configured_per_process_stores(settings: Any) -> None:
    """Refuse before composition (and before uvicorn forks, in ``run_server``)
    what the configuration alone decides: a SQLite database, then the
    process-local stores."""
    if serving_processes(settings) <= 1:
        return
    refuse_sqlite_across_workers(settings)
    refuse_per_process_stores(settings, configured_per_process_stores(settings))


def refuse_partial_composition(settings: Any, error: BaseException) -> None:
    """Refuse a partial API (a tolerated composition failure) under several processes.

    What was built is unknown, and with no Redis client published the request
    middleware builds a FakeRedis in each process.

    Raises:
        PerProcessStoresError: when several processes would serve.
    """
    processes = serving_processes(settings)
    if processes <= 1:
        return
    raise PerProcessStoresError(
        f"WORKERS={processes} runs {processes} server processes, and composition "
        f"failed ({error}), so each would serve a partial API on fallback stores "
        "of its own, a FakeRedis each among them. Set WORKERS=1 to run the "
        "partial API this deployment permits, or fix the failure."
    ) from error
