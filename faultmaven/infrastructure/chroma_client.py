"""ChromaDB client deployment gate.

The vector store has the same deployment split as Redis
(:mod:`faultmaven.infrastructure.redis_client`): standalone runs an in-process
``PersistentClient``; cloud runs a shared ChromaDB server over HTTP. A cloud
process that silently substitutes a ``PersistentClient`` writes vectors into
its own container filesystem — for a web pod that is per-replica search
results, and for a Job (``kb_seed``) it is durable-state corruption: the
Postgres rows land in the shared database while the vectors die with the pod,
leaving a KB that lists fine and searches empty (#901).

This module holds the pieces both client-acquisition paths (the container
provider and ``KnowledgeIngester``) and the deployment coherence gate share,
so they cannot drift apart on what "external ChromaDB is configured" means —
the #881 lesson applied to the vector store. It is also the one door every
``chromadb.HttpClient`` in ``faultmaven/`` is built through
(:func:`build_http_client`), which is where the credential and the request
timeout are applied (#2056, #1462).
"""

from __future__ import annotations

import logging
import string
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from chromadb.api import ClientAPI

# The shared predicate lives in the config module (beside coherence check 7)
# so the import stays one-directional: infrastructure → config. Re-exported
# here because this module is the client factories' natural import surface.
from faultmaven.config.deployment_coherence import (
    CHROMA_STORAGE_SYNONYMS,
    DeploymentCoherenceError,
    is_external_chroma_configured,
    is_host_only_chroma_configured,
)

__all__ = [
    "CHROMA_EXECUTOR",
    "CHROMA_HTTP_TIMEOUT_SECONDS",
    "CHROMA_STORAGE_SYNONYMS",
    "CHROMA_TOKEN_AUTH_PROVIDER",
    "ChromaClientConfigError",
    "ChromaUnavailableError",
    "build_http_client",
    "chroma_token_auth_kwargs",
    "is_external_chroma_configured",
    "is_host_only_chroma_configured",
    "is_http_client",
    "is_server_backed",
    "local_chroma_or_fail",
]

logger = logging.getLogger(__name__)

#: The client-side bound on every ``HttpClient`` request, in seconds.
#:
#: chromadb builds its session as ``httpx.Client(timeout=None)`` and exposes no
#: setting for it, so without this a stalled server holds the calling thread
#: for as long as it stalls (#2056). The value is the longest single-request
#: budget any vector store declares (``add_documents``,
#: ``call_external(timeout=30.0)``), so no request is cut shorter than its own
#: budget. ``call_external``'s ``wait_for`` still ends each *await* at that
#: call's own budget; this bounds the *thread* the request runs on, which
#: ``wait_for`` cannot.
CHROMA_HTTP_TIMEOUT_SECONDS = 30.0

#: Where the vector stores run their ChromaDB requests on the ``HttpClient``
#: arm, so that a slow server never holds the event loop (#2056).
#:
#: Dedicated, not the default executor: the default pool also runs BGE-M3
#: ``encode`` and Presidio on the upload path, and a Chroma stall must queue
#: only Chroma calls. Like ``component_monitor._PROBE_EXECUTOR``, a stalled
#: request strands one worker until :data:`CHROMA_HTTP_TIMEOUT_SECONDS` ends
#: it; once the pool is full a further call is queued, and is cancelled
#: unstarted when its ``wait_for`` expires. The local ``PersistentClient`` never
#: runs here: its SQLite connection is bound to the thread that created it.
CHROMA_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="chroma-http")

# The client-side token auth provider. ``chromadb.auth.token`` was renamed to
# ``chromadb.auth.token_authn`` in ChromaDB 0.5 — below our ``>= 1.0.0`` floor,
# so the old path never imports and must not appear anywhere. It once did, in
# the container provider, guarded by ``except Exception: pass`` — which
# silently produced an unauthenticated client for over a year (#1173).
CHROMA_TOKEN_AUTH_PROVIDER = "chromadb.auth.token_authn.TokenAuthClientProvider"

# chromadb's own rule (``token_authn._check_token`` at the 1.5.8 pin) —
# mirrored here so a bad credential fails before a client exists, not inside
# the callers' connection-failure handling. See chroma_token_auth_kwargs.
_VALID_TOKEN_CHARS = frozenset(
    string.digits + string.ascii_letters + string.punctuation
)


class ChromaClientConfigError(ValueError):
    """A ChromaDB client that must not be built, for a reason that is not connectivity.

    No caller may answer it with a fallback or a degraded mode: both client
    acquisition paths re-raise it ahead of their connection-failure handling.
    That handling exists for an unreachable server, and answering a
    configuration fault with it swaps the populated external store for an
    empty local one on standalone, and blames connectivity on cloud (#1173,
    #2056). A ``ValueError`` so the callers that caught the malformed-token
    ``ValueError`` before this class existed still do.
    """


def chroma_token_auth_kwargs(settings: Any) -> dict[str, str]:
    """Client-side token-auth ``ChromaSettings`` kwargs, or ``{}`` if no token.

    The single place every HTTP client resolves its ChromaDB credential —
    :func:`build_http_client` calls it, and both acquisition paths (the
    container provider and ``KnowledgeIngester``) build through that — so
    they cannot drift on either axis again — before #1173 one path read
    ``chromadb_api_key`` with a dead provider path while the other read
    ``chromadb_auth_token`` with the correct one, and neither actually sent a
    token on the deployed configuration. ``CHROMADB_AUTH_TOKEN`` is canonical;
    ``CHROMADB_API_KEY`` is accepted because the deployed secrets carry the
    same value under both names (see faultmaven-enterprise-infra
    ``base/secrets.yaml``).

    No import probe and no ``try`` around this: the provider module exists at
    every supported chromadb version, and if the provider path ever breaks,
    client construction must raise — under cloud that is a boot refusal, which
    is the correct failure direction for "a token is configured but cannot be
    sent" (#1173: both halves failing open is what hid this).

    The token is validated HERE, not left to ``TokenAuthClientProvider``
    inside ``HttpClient(...)``: the provider's ``ValueError`` fires inside the
    callers' connection-failure handling, where standalone answers it with a
    silent ``PersistentClient`` fallback (the populated external store swapped
    for an empty local one) and cloud with a boot refusal whose headline
    blames connectivity. It is raised as :class:`ChromaClientConfigError`
    before any client is constructed, and both acquisition paths re-raise that
    class ahead of their connection-failure handling, so a malformed
    credential stays loud and named on both. Surrounding whitespace
    is stripped first — the deployed secret is minted with shell tooling, and
    a trailing newline should not become a fleet-wide CrashLoop — but any
    interior violation of chromadb's own character rule (ASCII letters,
    digits, punctuation; ``token_authn._check_token``) is refused.
    """
    auth_token = settings.database.chromadb_auth_token
    api_key = settings.database.chromadb_api_key
    if (
        auth_token
        and api_key
        and auth_token.get_secret_value() != api_key.get_secret_value()
    ):
        # Precedence is silent otherwise, and the loser is the spelling most
        # of the docs mention — an operator who rotated CHROMADB_API_KEY
        # would send the stale token with no signal until enforcement exists.
        logger.warning(
            "CHROMADB_AUTH_TOKEN and CHROMADB_API_KEY are both set and "
            "differ; using CHROMADB_AUTH_TOKEN. The deployed secrets are "
            "expected to carry the same value under both names."
        )
    secret = auth_token or api_key
    if not secret:
        return {}
    source = "CHROMADB_AUTH_TOKEN" if auth_token else "CHROMADB_API_KEY"
    token = secret.get_secret_value().strip()
    if not token or not all(c in _VALID_TOKEN_CHARS for c in token):
        raise ChromaClientConfigError(
            f"{source} is set but is not a usable ChromaDB token: it must be "
            "non-empty and contain only ASCII letters, digits, and "
            "punctuation. Fix the credential; an unauthenticated client is "
            "not an acceptable fallback (#1173)."
        )
    return {
        "chroma_client_auth_provider": CHROMA_TOKEN_AUTH_PROVIDER,
        "chroma_client_auth_credentials": token,
    }


class ChromaUnavailableError(DeploymentCoherenceError):
    """Raised when the external ChromaDB is unusable on a deployment that requires it.

    A subclass of :class:`DeploymentCoherenceError` — the same boot-refusal
    signal the deployment coherence gate raises, because this is the same
    statement: the running configuration contradicts ``DEPLOYMENT_MODE``.
    """


def local_chroma_or_fail(reason: str, settings: Any) -> None:
    """Allow a local ``PersistentClient``, or refuse when cloud requires the server.

    A ``PersistentClient`` lives in ONE container filesystem. Under
    ``DEPLOYMENT_MODE=cloud`` the vector store is deployment-wide state, so
    substituting a local client silently forks it per-process: a seeding Job
    reports success while the shared server stays empty, and no signal an
    operator has says otherwise. Cloud refuses to run instead. Standalone is
    single-process, where ``PersistentClient`` is the intended backend, so
    callers proceed (with a warning only when an external server was
    configured but unreachable — the caller logs that before calling here).

    Returns normally when the fallback is permitted; raises
    :class:`ChromaUnavailableError` when it is not.
    """
    if settings.is_cloud:
        raise ChromaUnavailableError(
            f"External ChromaDB is required under DEPLOYMENT_MODE=cloud but is "
            f"unusable: {reason}. Refusing to fall back to a local "
            "PersistentClient — vectors would be written into this container's "
            "filesystem and silently diverge from the shared server (rows "
            "without vectors, per-replica search results). Check CHROMADB_URL / "
            "VECTOR_STORAGE_TYPE and connectivity to the ChromaDB service."
        )


def is_server_backed(client: Any) -> bool:
    """Whether this client talks to a ChromaDB *server* rather than a local tree.

    Asked of the client that was actually created, never of the configuration.
    ``chromadb.HttpClient`` raises at construction when the server is
    unreachable, so the container's factory catches that and — on standalone —
    falls back to a local ``PersistentClient``. A caller that inferred "this is
    the external server" from ``CHROMADB_URL`` being set would then mislabel a
    local tree as the shared server.

    ``chroma_server_host`` is populated by ``HttpClient`` and left unset by
    ``PersistentClient`` (verified against the pinned chromadb). Unknown shapes
    answer ``False``: a caller sweeping stores then keeps BOTH local clients
    instead of collapsing to one.
    """
    try:
        return bool(getattr(client.get_settings(), "chroma_server_host", None))
    except Exception:
        return False


def build_http_client(
    settings: Any, *, host: str, port: int, **settings_overrides: Any
) -> ClientAPI:
    """Build a ``chromadb.HttpClient``: the one door every one in ``faultmaven/`` uses.

    It applies the two things no HTTP client may be built without, so a new
    acquisition site cannot skip either (#1462;
    ``tests/unit/infrastructure/test_chroma_http_client_one_door.py`` fails on
    any other construction site):

    - **the shared credential**, resolved HERE by
      :func:`chroma_token_auth_kwargs` before any client is constructed, so a
      malformed one is a :class:`ChromaClientConfigError`, never a connection
      failure;
    - **the request timeout**, :data:`CHROMA_HTTP_TIMEOUT_SECONDS`, set on the
      client's own ``httpx`` session. chromadb builds that session with
      ``timeout=None`` and offers no setting for it, and each ``HttpClient()``
      has its own session, so it is set on every client this builds (#2056).

    ``settings_overrides`` are further ``ChromaSettings`` fields (the ingester
    passes ``anonymized_telemetry=False, allow_reset=True``).

    Construction itself makes three round trips to the server, before the
    timeout is set; those are boot-time, and the callers bound them their own
    way. ``chromadb.HttpClient`` raises when the server is unreachable, which
    the callers handle as connectivity.

    Raises:
        ChromaClientConfigError: a malformed credential, or a chromadb whose
            client exposes no HTTP session to bound. The second fails a
            chromadb upgrade that moves the session loudly, rather than
            shipping requests with no timeout.
    """
    import chromadb
    import httpx
    from chromadb.config import Settings as ChromaSettings

    auth_kwargs = chroma_token_auth_kwargs(settings)
    client = chromadb.HttpClient(
        host=host,
        port=port,
        settings=ChromaSettings(**auth_kwargs, **settings_overrides),
    )
    session = getattr(getattr(client, "_server", None), "_session", None)
    if session is None:
        raise ChromaClientConfigError(
            f"chromadb {getattr(chromadb, '__version__', '?')} exposes no HTTP "
            "session to bound (client._server._session): its requests would "
            "run with no timeout and hold their thread for as long as the "
            "server stalls. Update build_http_client for this chromadb "
            "version (#2056)."
        )
    session.timeout = httpx.Timeout(CHROMA_HTTP_TIMEOUT_SECONDS)
    return client


def is_http_client(client: Any) -> bool:
    """Whether this client sends its requests over HTTP, to a ChromaDB server.

    Decides where a vector store runs its ChromaDB calls: an HTTP client's
    run in :data:`CHROMA_EXECUTOR`, off the event loop; a ``PersistentClient``'s
    stay on the loop thread, which its SQLite connection requires. Asked of
    the client object rather than of the settings, because standalone falls
    back to a ``PersistentClient`` when the configured URL is unreachable.
    ``False`` for ``None`` and for any client that is not chromadb's HTTP one.
    """
    # The module, not ``from … import FastAPI``: that import is a shape the
    # one-door scan refuses outside build_http_client, and this is a type
    # test, not a construction.
    import chromadb.api.fastapi

    return isinstance(getattr(client, "_server", None), chromadb.api.fastapi.FastAPI)
