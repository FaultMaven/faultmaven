"""Both ChromaDB HTTP client paths must send the configured token (#1173).

Before this fix the deployment sent no credentials from either path, and each
half hid the other — the server enforced nothing, so everything worked:

- The container provider imported the pre-0.5 module path
  ``chromadb.auth.token`` inside a bare ``except Exception: pass``. At every
  supported chromadb version the import raises, the failure was swallowed, and
  the client was built unauthenticated.
- ``KnowledgeIngester`` had the correct ``token_authn`` path — but only on its
  ``CHROMADB_HOST`` branch, while the deployed configuration sets
  ``CHROMADB_URL`` and took the branch that passed no auth settings at all.
  It also read a different setting (``chromadb_auth_token``) than the
  container path (``chromadb_api_key``), with a hardcoded fallback token.

These tests pin the repaired invariant: one shared resolver, both paths, both
setting spellings, credentials actually placed on the client settings — and
the provider path importable at the pinned chromadb version, so a revert to
the dead path fails a test instead of failing silently in production.
"""

from importlib import import_module

import chromadb
import pytest
from pydantic import SecretStr

from faultmaven.config.settings import DeploymentMode, FaultMavenSettings
from faultmaven.container.providers.infrastructure import _create_chromadb_client
from faultmaven.infrastructure.chroma_client import (
    CHROMA_TOKEN_AUTH_PROVIDER,
    chroma_token_auth_kwargs,
)
from faultmaven.modules.knowledge.domain.services.ingestion import KnowledgeIngester

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _settings(
    *,
    chromadb_url: str = "",
    chromadb_host: str = "localhost",
    auth_token: str | None = None,
    api_key: str | None = None,
) -> FaultMavenSettings:
    settings = FaultMavenSettings(_env_file=None)
    settings.deployment_mode = DeploymentMode.STANDALONE
    # Explicit, not ambient — a developer's exported CHROMADB_* must not
    # decide which branch these tests exercise (same rule as the cloud-gate
    # tests in test_chromadb_provider_cloud_gate.py).
    settings.database.chromadb_url = chromadb_url
    settings.database.chromadb_host = chromadb_host
    settings.database.vector_storage_type = "chromadb"
    settings.database.chromadb_auth_token = (
        SecretStr(auth_token) if auth_token else None
    )
    settings.database.chromadb_api_key = SecretStr(api_key) if api_key else None
    settings.server.skip_service_checks = False
    return settings


class _CapturingHttpClient:
    """Records the ChromaSettings it was constructed with, acts connected."""

    captured: list[dict] = []

    def __init__(self, *args, **kwargs):
        type(self).captured.append(kwargs)

    def get_or_create_collection(self, *args, **kwargs):
        return object()


@pytest.fixture(autouse=True)
def _reset_capture():
    _CapturingHttpClient.captured = []


# --------------------------------------------------------------------------- #
# The shared resolver
# --------------------------------------------------------------------------- #


def test_provider_path_imports_at_the_pinned_chromadb_version():
    """The dead-path trap: ``chromadb.auth.token`` never imported at any
    supported version, and the swallow around it made that invisible. The
    provider string we ship must resolve to a real class, verified by
    importing it — not by reading it."""
    module_path, class_name = CHROMA_TOKEN_AUTH_PROVIDER.rsplit(".", 1)
    module = import_module(module_path)
    assert hasattr(module, class_name)


def test_no_token_configured_sends_nothing():
    assert chroma_token_auth_kwargs(_settings()) == {}


def test_auth_token_setting_is_used():
    kwargs = chroma_token_auth_kwargs(_settings(auth_token="sekrit"))
    assert kwargs == {
        "chroma_client_auth_provider": CHROMA_TOKEN_AUTH_PROVIDER,
        "chroma_client_auth_credentials": "sekrit",
    }


def test_api_key_spelling_is_accepted():
    """The deployed secrets carry the same token under both names; the
    container path historically read only this one."""
    kwargs = chroma_token_auth_kwargs(_settings(api_key="sekrit"))
    assert kwargs["chroma_client_auth_credentials"] == "sekrit"


def test_auth_token_wins_over_api_key():
    kwargs = chroma_token_auth_kwargs(
        _settings(auth_token="canonical", api_key="legacy")
    )
    assert kwargs["chroma_client_auth_credentials"] == "canonical"


# --------------------------------------------------------------------------- #
# Container provider path
# --------------------------------------------------------------------------- #


def test_container_client_carries_the_token(monkeypatch):
    monkeypatch.setattr(chromadb, "HttpClient", _CapturingHttpClient)
    settings = _settings(chromadb_url="http://chromadb:8000", api_key="sekrit")

    _create_chromadb_client(settings, "./data/ignored", "KB")

    (call,) = _CapturingHttpClient.captured
    chroma_settings = call["settings"]
    assert chroma_settings.chroma_client_auth_provider == CHROMA_TOKEN_AUTH_PROVIDER
    assert chroma_settings.chroma_client_auth_credentials == "sekrit"


def test_container_client_without_token_sends_no_credentials(monkeypatch):
    monkeypatch.setattr(chromadb, "HttpClient", _CapturingHttpClient)
    settings = _settings(chromadb_url="http://chromadb:8000")

    _create_chromadb_client(settings, "./data/ignored", "KB")

    (call,) = _CapturingHttpClient.captured
    assert call["settings"].chroma_client_auth_provider is None


# --------------------------------------------------------------------------- #
# KnowledgeIngester — BOTH http branches, because the deployed configuration
# (CHROMADB_URL set) took the one that used to send nothing
# --------------------------------------------------------------------------- #


def test_ingester_url_branch_carries_the_token(monkeypatch):
    monkeypatch.setattr(chromadb, "HttpClient", _CapturingHttpClient)
    settings = _settings(chromadb_url="http://chromadb:8000", auth_token="sekrit")

    ingester = KnowledgeIngester(settings=settings)

    assert ingester.degraded is False
    (call,) = _CapturingHttpClient.captured
    chroma_settings = call["settings"]
    assert chroma_settings.chroma_client_auth_provider == CHROMA_TOKEN_AUTH_PROVIDER
    assert chroma_settings.chroma_client_auth_credentials == "sekrit"


def test_ingester_host_branch_carries_the_token(monkeypatch):
    monkeypatch.setattr(chromadb, "HttpClient", _CapturingHttpClient)
    settings = _settings(chromadb_host="chromadb.faultmaven.svc", auth_token="sekrit")

    KnowledgeIngester(settings=settings)

    (call,) = _CapturingHttpClient.captured
    assert call["settings"].chroma_client_auth_credentials == "sekrit"


def test_ingester_without_token_sends_nothing_not_a_default(monkeypatch):
    """The old code fell back to a hardcoded dev token. No configured token
    must now mean NO credentials — a guessable default sent silently is the
    same fail-open shape #1173 removes."""
    monkeypatch.setattr(chromadb, "HttpClient", _CapturingHttpClient)
    settings = _settings(chromadb_url="http://chromadb:8000")

    KnowledgeIngester(settings=settings)

    (call,) = _CapturingHttpClient.captured
    chroma_settings = call["settings"]
    assert chroma_settings.chroma_client_auth_provider is None
    assert chroma_settings.chroma_client_auth_credentials is None
