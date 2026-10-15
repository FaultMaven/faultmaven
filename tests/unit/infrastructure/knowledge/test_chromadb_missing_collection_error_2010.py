"""A missing collection raises ``chromadb.errors.NotFoundError`` (#2010).

Both vector stores import ``NotFoundError`` and treat it as "no collection".
``pyproject.toml`` once declared ``chromadb>=0.5.3``, where the name does not
import (<= 0.5.5) or ``get_collection`` raises something else (0.5.7 - 0.6.3:
``InvalidCollectionException``), so an install near the floor could not use the
stores. The floor is the earliest release that exports the class AND raises
it, found by running the releases (1.0.0). This pins the installed client's
behaviour so a lockfile move to a client that raises another type fails here.
"""

import tempfile
import tomllib
from pathlib import Path

import chromadb
import pytest
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
    client = chromadb.EphemeralClient()
    with pytest.raises(NotFoundError):
        client.get_collection("missing-2010")


def test_persistent_client_raises_not_found_for_a_missing_collection():
    with tempfile.TemporaryDirectory() as path:
        client = chromadb.PersistentClient(path)
        with pytest.raises(NotFoundError):
            client.get_collection("missing-2010")


def test_the_declared_floor_admits_only_versions_that_raise_not_found():
    """0.6.3 and below raise ``InvalidCollectionException`` (or cannot import
    the class), so the declared specifier must exclude them and admit 1.0.0."""
    root = Path(__file__).resolve().parents[4]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    (requirement,) = [
        Requirement(r)
        for r in project["dependencies"]
        if Requirement(r).name == "chromadb"
    ]

    assert not requirement.specifier.contains(Version("0.6.3"), prereleases=True)
    assert not requirement.specifier.contains(Version("0.5.3"), prereleases=True)
    assert requirement.specifier.contains(Version("1.0.0"))
