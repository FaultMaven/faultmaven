"""A missing collection raises ``chromadb.errors.NotFoundError`` (#2010).

Both vector stores import ``NotFoundError`` and treat it as "no collection".
``pyproject.toml`` once declared ``chromadb>=0.5.3``, where the name does not
import (<= 0.5.5) or ``get_collection`` raises something else (0.5.7 - 0.6.3:
``InvalidCollectionException``), so an install near the floor could not use the
stores. The earliest release that exports the class AND raises it is 1.0.0,
found by running the releases. The floor now sits higher, at 1.5.2, for
``Client.close()`` (``tests/unit/container/test_container_shutdown.py``). This
pins the installed client's behaviour so a lockfile move to a client that
raises another type fails here.
"""

import tempfile
import tomllib
from pathlib import Path

import chromadb
import pytest
from chromadb.config import Settings as ChromaSettings
from chromadb.errors import NotFoundError
from packaging.requirements import Requirement
from packaging.version import Version

import faultmaven.infrastructure.knowledge.knowledge_vector_store as knowledge_store
import faultmaven.infrastructure.persistence.case_vector_store as case_store

pytestmark = pytest.mark.unit


def test_both_stores_catch_the_class_the_client_raises():
    assert knowledge_store.NotFoundError is NotFoundError
    assert case_store.NotFoundError is NotFoundError


def test_ephemeral_client_raises_not_found_for_a_missing_collection():
    # Pinned settings: a bare ``EphemeralClient()`` is order-fragile (#823).
    # chromadb caches one System per identifier and refuses a second client
    # whose ``Settings`` differ, and ``Settings.environment`` follows the
    # ambient ENVIRONMENT other tests change.
    client = chromadb.EphemeralClient(
        settings=ChromaSettings(
            anonymized_telemetry=False,
            allow_reset=False,
            environment="",
            is_persistent=False,
        )
    )
    with pytest.raises(NotFoundError):
        client.get_collection("missing-2010")


def test_persistent_client_raises_not_found_for_a_missing_collection():
    # Closed before the directory goes. Left open, the client would keep its
    # deleted files' descriptors for the rest of the process. Closed, it keeps
    # only chromadb's SQLite handles, until it is garbage-collected.
    with (
        tempfile.TemporaryDirectory() as path,
        chromadb.PersistentClient(path) as client,
    ):
        with pytest.raises(NotFoundError):
            client.get_collection("missing-2010")


def test_the_declared_floor_admits_only_versions_that_raise_not_found():
    """0.6.3 and below raise ``InvalidCollectionException`` (or cannot import
    the class), so the declared specifier must exclude them."""
    root = Path(__file__).resolve().parents[4]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    (requirement,) = [
        Requirement(r)
        for r in project["dependencies"]
        if Requirement(r).name == "chromadb"
    ]

    assert not requirement.specifier.contains(Version("0.6.3"), prereleases=True)
    assert not requirement.specifier.contains(Version("0.5.3"), prereleases=True)
