"""The ChromaDB client must not fail open to a local PersistentClient under cloud (#901).

The provider used to answer an unreachable ChromaDB server with a WARNING and a
``PersistentClient`` inside the calling pod. On the #629 flip rehearsal that let
``kb_seed`` write 91 runbooks' vectors into an ephemeral Job container, log
``failed=0`` and exit 0 — while the shared server held zero vectors and the
Postgres rows landed in the shared database. Last of the fail-open trio after
#885 (container init) and #893/#895 (FakeRedis).

Cloud must refuse (``ChromaUnavailableError``, a ``DeploymentCoherenceError``,
so the same boot-refusal path as the Redis gate); standalone keeps the
fallback — ``PersistentClient`` is the intended backend there.

Settings are the REAL ``FaultMavenSettings``: a stand-in's ``is_cloud`` is
truthy in both modes, which would let the standalone half of each pair pass
against a dead gate.
"""

import chromadb
import pytest

from faultmaven.config.settings import DeploymentMode, FaultMavenSettings
from faultmaven.container.providers.infrastructure import _create_chromadb_client
from faultmaven.container.providers.tools import create_knowledge_ingester
from faultmaven.infrastructure.chroma_client import (
    ChromaUnavailableError,
    is_external_chroma_configured,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _settings(
    *,
    cloud: bool,
    chromadb_url: str = "",
    vector_storage_type: str = "chromadb",
) -> FaultMavenSettings:
    settings = FaultMavenSettings(_env_file=None)
    settings.deployment_mode = (
        DeploymentMode.CLOUD if cloud else DeploymentMode.STANDALONE
    )
    # Set the vector config explicitly — ambient env (a developer's exported
    # CHROMADB_URL or SKIP_SERVICE_CHECKS) must not decide which branch these
    # tests exercise.
    settings.database.chromadb_url = chromadb_url
    settings.database.vector_storage_type = vector_storage_type
    settings.server.skip_service_checks = False
    return settings


class _ExplodingHttpClient:
    """Stand-in for an unreachable server: construction raises, like the real
    HttpClient does on connect timeout."""

    def __init__(self, *args, **kwargs):
        raise ValueError("[Errno 110] Connection timed out")


# --------------------------------------------------------------------------- #
# Cloud: refusal
# --------------------------------------------------------------------------- #


def test_cloud_unreachable_server_refuses_instead_of_falling_back(monkeypatch):
    monkeypatch.setattr(chromadb, "HttpClient", _ExplodingHttpClient)
    settings = _settings(cloud=True, chromadb_url="http://chromadb:8000")
    assert settings.is_cloud is True

    with pytest.raises(ChromaUnavailableError, match="PersistentClient"):
        _create_chromadb_client(settings, "./data/ignored", "KB")


def test_cloud_without_chromadb_url_refuses(monkeypatch):
    """The quiet path — no URL, no probe, straight to PersistentClient — must
    refuse too, not just the probe-failure path."""

    def _must_not_be_called(*args, **kwargs):  # pragma: no cover - the assertion
        raise AssertionError("HttpClient must not be probed when no URL is set")

    monkeypatch.setattr(chromadb, "HttpClient", _must_not_be_called)
    settings = _settings(cloud=True, chromadb_url="")

    with pytest.raises(ChromaUnavailableError, match="CHROMADB_URL"):
        _create_chromadb_client(settings, "./data/ignored", "KB")


def test_cloud_legacy_storage_type_refuses():
    """A legacy VECTOR_STORAGE_TYPE deselects the external server even with a
    URL set — under cloud that is the same refusal, not a silent local store."""
    settings = _settings(
        cloud=True, chromadb_url="http://chromadb:8000", vector_storage_type="inmemory"
    )

    with pytest.raises(ChromaUnavailableError):
        _create_chromadb_client(settings, "./data/ignored", "KB")


def test_cloud_reachable_server_returns_the_http_client(monkeypatch):
    """The gate must be able to PASS: with a reachable server, cloud gets the
    HttpClient and no refusal — proves the refusal branch is reachable-only."""
    sentinel = object()
    monkeypatch.setattr(chromadb, "HttpClient", lambda **kwargs: sentinel)
    settings = _settings(cloud=True, chromadb_url="http://chromadb:8000")

    client = _create_chromadb_client(settings, "./data/ignored", "KB")

    assert client is sentinel


# --------------------------------------------------------------------------- #
# Standalone: the fallback is the intended backend
# --------------------------------------------------------------------------- #


def test_standalone_unreachable_server_falls_back_to_persistent(monkeypatch, tmp_path):
    monkeypatch.setattr(chromadb, "HttpClient", _ExplodingHttpClient)
    settings = _settings(cloud=False, chromadb_url="http://chromadb:8000")

    client = _create_chromadb_client(settings, str(tmp_path / "chroma"), "KB")

    assert type(client).__name__ != "_ExplodingHttpClient"
    assert (tmp_path / "chroma").exists()


def test_standalone_without_url_gets_persistent_client(tmp_path):
    settings = _settings(cloud=False, chromadb_url="")

    client = _create_chromadb_client(settings, str(tmp_path / "chroma"), "KB")

    assert client is not None
    assert (tmp_path / "chroma").exists()


# --------------------------------------------------------------------------- #
# The shared predicate — both dispatch sites and the coherence gate read this
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("url", "storage_type", "expected"),
    [
        ("http://chromadb:8000", "chromadb", True),
        ("http://chromadb:8000", "chroma", True),
        ("http://chromadb:8000", "ChromaDB", True),  # case-insensitive
        ("", "chromadb", False),
        ("   ", "chromadb", False),
        ("http://chromadb:8000", "inmemory", False),
        ("http://chromadb:8000", "", False),
    ],
)
def test_is_external_chroma_configured(url, storage_type, expected):
    settings = _settings(cloud=True, chromadb_url=url, vector_storage_type=storage_type)
    assert is_external_chroma_configured(settings) is expected


# --------------------------------------------------------------------------- #
# The third acquisition path: KnowledgeIngester (and its container wrapper)
# --------------------------------------------------------------------------- #


def test_knowledge_ingester_local_branch_refuses_under_cloud():
    """KnowledgeIngester builds its own client; its PersistentClient branch
    (no URL, localhost host) must hit the same gate, not bypass it (#894
    lesson: the jobs path and web path must compose identically)."""
    from faultmaven.modules.knowledge.domain.services.ingestion import (
        KnowledgeIngester,
    )

    settings = _settings(cloud=True, chromadb_url="")
    settings.database.chromadb_host = "localhost"

    with pytest.raises(ChromaUnavailableError):
        KnowledgeIngester(settings=settings)


def test_create_knowledge_ingester_propagates_the_refusal(monkeypatch):
    """The container wrapper catches Exception and returns None — a refusal
    swallowed into None would re-open exactly the fail-open path. It must
    propagate; ordinary construction failures still degrade to None."""
    settings = _settings(cloud=True, chromadb_url="")
    settings.database.chromadb_host = "localhost"

    with pytest.raises(ChromaUnavailableError):
        create_knowledge_ingester(settings)


def test_create_knowledge_ingester_still_softens_ordinary_failures(monkeypatch):
    import faultmaven.modules.knowledge.domain.services.ingestion as ingestion_mod

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("BGE-M3 model unavailable")

    monkeypatch.setattr(ingestion_mod, "KnowledgeIngester", _Boom)
    settings = _settings(cloud=False)

    assert create_knowledge_ingester(settings) is None
