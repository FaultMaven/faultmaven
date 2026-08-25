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

from faultmaven.container.providers.infrastructure import _create_chromadb_client
from faultmaven.infrastructure.chroma_client import (
    CHROMA_TOKEN_AUTH_PROVIDER,
    chroma_token_auth_kwargs,
)
from faultmaven.modules.knowledge.domain.services.ingestion import KnowledgeIngester
from tests.unit.container.conftest import make_chroma_settings as _settings

pytestmark = [pytest.mark.unit, pytest.mark.security]


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


def test_both_set_and_different_warns(caplog):
    """Precedence is silent otherwise, and the loser is the spelling most of
    the docs mention — a rotated CHROMADB_API_KEY shadowed by a stale
    CHROMADB_AUTH_TOKEN would send the stale token with no signal."""
    with caplog.at_level("WARNING"):
        chroma_token_auth_kwargs(_settings(auth_token="canonical", api_key="legacy"))
    assert any(
        "both set" in r.message and "differ" in r.message for r in caplog.records
    )


def test_both_set_and_equal_does_not_warn(caplog):
    """The deployed secrets carry the same value under both names — the
    expected configuration must stay quiet. Settings are built OUTSIDE the
    caplog block and the assertion is scoped to THIS warning: settings
    construction logs about unrelated ambient env (CI exports
    OAUTH_ENABLED=true, which warns under the default AUTH_MODE=local), and a
    bare `not caplog.records` fails on any of it."""
    settings = _settings(auth_token="same", api_key="same")
    with caplog.at_level("WARNING"):
        chroma_token_auth_kwargs(settings)
    assert not [r for r in caplog.records if "both set" in r.message]


def test_surrounding_whitespace_is_stripped():
    """An echo-minted k8s secret ends in a newline; that must not become a
    fleet-wide boot refusal when the credential is otherwise fine."""
    kwargs = chroma_token_auth_kwargs(_settings(auth_token="sekrit\n"))
    assert kwargs["chroma_client_auth_credentials"] == "sekrit"


@pytest.mark.parametrize("bad", ["se krit", "sekrité", "   \n"])
def test_malformed_token_raises_a_named_error(bad):
    """Validated HERE, before any client exists: TokenAuthClientProvider's own
    ValueError fires inside the callers' connection-failure handling, where
    standalone answers with a silent PersistentClient fallback and cloud with
    a boot refusal that blames connectivity. The error must name the env var
    that carried the bad value."""
    with pytest.raises(ValueError, match="CHROMADB_AUTH_TOKEN"):
        chroma_token_auth_kwargs(_settings(auth_token=bad))
    with pytest.raises(ValueError, match="CHROMADB_API_KEY"):
        chroma_token_auth_kwargs(_settings(api_key=bad))


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


def test_container_malformed_token_raises_instead_of_falling_back(tmp_path):
    """A configured-but-unusable token must be loud on standalone too. Left
    to TokenAuthClientProvider, the ValueError fires inside the HttpClient
    try block, and standalone answers a construction failure there with a
    silent local PersistentClient — the populated external store swapped for
    an empty one, the fail-open shape #1173 removes. The helper validates
    before any client exists, so the error escapes instead."""
    settings = _settings(chromadb_url="http://chromadb:8000", auth_token="bad token")

    with pytest.raises(ValueError, match="CHROMADB_AUTH_TOKEN"):
        _create_chromadb_client(settings, str(tmp_path / "chroma"), "KB")

    assert not (tmp_path / "chroma").exists()


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


def test_ingester_url_without_explicit_port_uses_scheme_default(monkeypatch):
    """The old split(':')[-1] parse raised on any URL without an explicit
    port — e.g. an https URL behind a TLS terminator or auth proxy — and the
    ValueError escaped the degraded-mode guard, so the ingester silently
    vanished while the container path (urlparse, scheme defaults) connected
    fine. Both paths must read the same URL the same way."""
    monkeypatch.setattr(chromadb, "HttpClient", _CapturingHttpClient)
    settings = _settings(chromadb_url="https://chroma.internal", auth_token="sekrit")

    ingester = KnowledgeIngester(settings=settings)

    assert ingester.degraded is False
    (call,) = _CapturingHttpClient.captured
    assert call["host"] == "chroma.internal"
    assert call["port"] == 443


def test_ingester_dispatch_matches_the_container_predicate(monkeypatch, tmp_path):
    """A legacy VECTOR_STORAGE_TYPE deselects the external server for the
    container path; the ingester used to go over HTTP anyway — writes landing
    on a server where reads never look. Both paths now dispatch on
    is_external_chroma_configured."""

    def _must_not_be_called(*args, **kwargs):  # pragma: no cover - the assertion
        raise AssertionError("ingester must not open HTTP when deselected")

    monkeypatch.setattr(chromadb, "HttpClient", _must_not_be_called)
    settings = _settings(
        chromadb_url="http://chromadb:8000",
        chromadb_host="chromadb.faultmaven.svc",
        vector_storage_type="inmemory",
    )
    settings.database.chromadb_kb_persist_dir = str(tmp_path / "chroma-kb")

    ingester = KnowledgeIngester(settings=settings)

    assert ingester.chroma_client is not None
    assert (tmp_path / "chroma-kb").exists()


def test_ingester_host_branch_carries_the_token(monkeypatch):
    monkeypatch.setattr(chromadb, "HttpClient", _CapturingHttpClient)
    settings = _settings(chromadb_host="chromadb.faultmaven.svc", auth_token="sekrit")

    KnowledgeIngester(settings=settings)

    (call,) = _CapturingHttpClient.captured
    assert call["settings"].chroma_client_auth_credentials == "sekrit"


@pytest.mark.parametrize(
    "branch_settings",
    [
        {"chromadb_url": "http://chromadb:8000"},
        # The HOST branch is where the hardcoded fallback token lived — this
        # parametrization is what makes a revert of its removal fail.
        {"chromadb_host": "chromadb.faultmaven.svc"},
    ],
)
def test_ingester_without_token_sends_nothing_not_a_default(
    monkeypatch, branch_settings
):
    """The old code fell back to a hardcoded dev token. No configured token
    must now mean NO credentials — a guessable default sent silently is the
    same fail-open shape #1173 removes."""
    monkeypatch.setattr(chromadb, "HttpClient", _CapturingHttpClient)
    settings = _settings(**branch_settings)

    KnowledgeIngester(settings=settings)

    (call,) = _CapturingHttpClient.captured
    chroma_settings = call["settings"]
    assert chroma_settings.chroma_client_auth_provider is None
    assert chroma_settings.chroma_client_auth_credentials is None
