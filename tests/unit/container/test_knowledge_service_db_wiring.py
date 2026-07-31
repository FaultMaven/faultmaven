"""The container-built KnowledgeService must be able to reach the database.

``KnowledgeService.ingest_runbook`` refuses outright when
``_db_session_factory`` is unset ("vector-only ingestion is no longer
supported"), and every other KB persistence path degrades to a warning and an
empty result. The web process used to hide that by patching the attribute onto
the instance from the lifespan, so only the *web* process had a DB-capable
service. The jobs process (``python -m faultmaven.jobs.run``) initializes the
same container and never runs a lifespan, so ``kb_seed`` failed on every pack
runbook — and under ``TENANT_PROVIDER=multi`` that job is the only seeding
path, leaving a fresh deployment with an empty global KB (#894).

The fix is at the composition root: the container hands KnowledgeService the
session factory at construction, so every process gets the same capability.
These tests are deliberately source-level / provider-level — the boot-time
counterpart (a real ``container.initialize()``) lives in
``tests/integration/container/test_knowledge_service_db_wiring.py``.
"""

import re
from pathlib import Path

import pytest

from faultmaven.config.settings import FaultMavenSettings
from faultmaven.container.providers.services import create_knowledge_service
from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Composition roots and process entrypoints. Every one of these builds or boots
# a container; a post-construction patch in any of them re-creates the #894
# split where one process is DB-capable and the others silently are not.
_PATCH_SCAN_TARGETS = (
    _REPO_ROOT / "faultmaven" / "main.py",
    _REPO_ROOT / "faultmaven" / "bootstrap",
    _REPO_ROOT / "faultmaven" / "jobs",
    _REPO_ROOT / "faultmaven" / "api",
    _REPO_ROOT / "scripts",
    _REPO_ROOT / "demo",
)

# Attribute assignment on anything other than ``self`` — ``self._db_session_
# factory = ...`` is the constructor storing its own injected dependency.
# No ``\s*`` before ``=`` so ``ks._db_session_factory=f`` matches too.
_ATTR_PATCH = re.compile(r"(?<!self)\._db_session_factory\s*=")


@pytest.mark.unit
def test_knowledge_service_provider_always_wires_the_session_factory():
    """The provider factory itself carries the DB capability.

    The composition root previously relied on an optional parameter it never
    passed, which is how the bug shipped: the default silently produced a
    DB-less service. Real ``FaultMavenSettings`` here — the constructor reads
    them, and a stand-in would let this pass against a dead gate.
    """
    settings = FaultMavenSettings(_env_file=None)

    service = create_knowledge_service(
        vector_store=None,
        knowledge_ingester=None,
        sanitizer=None,
        tracer=None,
        llm_provider=None,
        redis_client=None,
        settings=settings,
    )

    assert service._db_session_factory is get_db_session


@pytest.mark.unit
def test_a_db_less_knowledge_service_cannot_be_constructed():
    """The capability is required at the constructor, not checked at use (#899).

    ``db_session_factory`` used to default to ``None``, and each persistence
    path carried its own guard — ``ingest_runbook`` raised, every read path
    returned empty. That made a DB-less service a *quiet* service: the #894
    container regression produced an empty global KB rather than a failure.
    Omitting the dependency is now a TypeError, which happens at container
    init, before anything serves or seeds.
    """
    with pytest.raises(TypeError, match="db_session_factory"):
        KnowledgeService(
            knowledge_ingester=None,
            sanitizer=None,
            tracer=None,
        )


@pytest.mark.unit
def test_container_does_not_substitute_a_stub_knowledge_service():
    """An uncomposed container returns None — it does not stand something in.

    It used to return ``MinimalKnowledgeService``: an in-memory fake that
    answered ``get_document`` with invented content ("This is sample document
    content for testing purposes.") for any plausible id, and had no
    ``ingest_runbook`` at all. Two consequences, both live: ``kb_seed`` and
    ``fm-reset-kb`` both guard on ``is None``, which the stub silently defeated
    (#899 item 3), and a partially composed *web* process served fabricated
    runbooks — a document the KB never contained, presented as one it did.

    Driving the getter on an uncomposed container, since that is the state the
    substitution used to hide. ``object.__new__`` rather than ``DIContainer()``
    or ``DIContainer.__new__(DIContainer)``: ``BaseDIContainer.__new__`` is a
    singleton, so both of those hand back whatever instance the session has
    already initialized — the assertion would then be reading another test's
    state instead of this getter's behavior.
    """
    from faultmaven._container_impl import DIContainer

    container = object.__new__(DIContainer)
    container._initialized = True  # skip lazy-init; we want the raw lookup
    assert "knowledge_service" not in container.__dict__

    assert container.get_knowledge_service() is None


@pytest.mark.unit
def test_no_post_construction_session_factory_patch_in_any_entrypoint():
    """No process may re-acquire the capability by monkey-patching it on.

    The capability belongs at the composition root, where every process — web,
    jobs, scripts — picks it up identically. A patch anywhere below hands it to
    exactly one process and hides a regression in the container wiring from all
    the others; that is precisely how ``kb_seed`` shipped broken (#894).

    Scanned as source rather than by import so a patch in a script that is
    never imported under test still gets caught.
    """
    offenders = []
    for target in _PATCH_SCAN_TARGETS:
        paths = [target] if target.is_file() else sorted(target.rglob("*.py"))
        for path in paths:
            source = path.read_text()
            for lineno, line in enumerate(source.splitlines(), start=1):
                if _ATTR_PATCH.search(line) or (
                    "setattr(" in line and "_db_session_factory" in line
                ):
                    offenders.append(
                        f"{path.relative_to(_REPO_ROOT)}:{lineno}: {line.strip()}"
                    )

    assert not offenders, (
        "These sites assign _db_session_factory onto a service after "
        f"construction: {offenders}. Wire it at the composition root "
        "(container/providers/services.py) instead, so every process — web, "
        "jobs, scripts — gets a DB-capable service (#894)."
    )
