"""Fixtures for the tests that drive a real ``container.initialize()``."""

import pytest


@pytest.fixture
async def fresh_container():
    """The DI container singleton, reset around the test, and shut down after it.

    These tests fully initialize the singleton every other test in the session
    shares, so they reset it on the way out too. Shutting it down first closes
    what the initialization opened: with service checks on, that is a real
    Redis client whose pool is bound to this test's event loop, and the KB and
    evidence ChromaDB clients.
    """
    from faultmaven.container import container

    container.reset()
    yield container
    await container.shutdown()
    container.reset()


@pytest.fixture
def checks_on_over_tmp_path_stores(monkeypatch, tmp_path):
    """Compose with ``SKIP_SERVICE_CHECKS=false``, whatever the ambient value.

    Every CI pytest job sets it ``true``, and under ``true`` both vector-store
    factories return None — so a pin that followed the ambient value would
    skip (or pass vacuously) in exactly the place it has to bite. This builds
    its own composition instead: standalone, with the two local ChromaDB
    ``PersistentClient`` trees in ``tmp_path``, so it needs no server and runs
    anywhere. The settings singleton is rebuilt from the patched environment
    and rebuilt again afterwards, before ``monkeypatch`` restores it.
    """
    from faultmaven.config.settings import reset_settings

    monkeypatch.setenv("SKIP_SERVICE_CHECKS", "false")
    monkeypatch.setenv("DEPLOYMENT_MODE", "standalone")
    # With checks on and protection on (the Cloud job), the composed
    # DataSanitizer health-checks the Presidio services at their default
    # in-cluster URLs, which is a real network call from a test that says it
    # needs no server. The composition under test has no use for them.
    monkeypatch.setenv("PROTECTION_ENABLED", "false")
    monkeypatch.setenv("SANITIZE_PII", "false")
    monkeypatch.setenv("CHROMADB_KB_PERSIST_DIR", str(tmp_path / "chroma-kb"))
    monkeypatch.setenv(
        "CHROMADB_EVIDENCE_PERSIST_DIR", str(tmp_path / "chroma-evidence")
    )
    reset_settings()
    yield
    reset_settings()
