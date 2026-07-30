"""Constructing a service must never load the embedding model (#868).

Every process that builds the DI container — the API pod *and* the cleanup
CronJobs — used to pull the ~1.3Gi BGE-M3 SentenceTransformer during
construction, because two constructors called ``get_bge_m3_model()``:
``SemanticCache`` (built by ``LLMRouter``) and ``KnowledgeIngester``. A
``storage_cleanup`` job that embeds nothing was OOMKilled at its 512Mi limit,
which is how every app CronJob failed silently for 112 days (infra#131).

The invariant these tests pin: **construction is not embedding.** Whether the
model is resident is decided by the documented policy (``LAZY_LOAD_ML_MODELS`` /
``PRELOAD_MODELS``, applied in the web lifespan) or by the first real
``aembed_*`` call — never as a side effect of building an object.

The probe is ``SentenceTransformer`` itself rather than ``get_bge_m3_model``:
that catches *any* path to a load, including one added later through a third
constructor. ``SENTENCE_TRANSFORMERS_AVAILABLE`` is forced True so the assertion
cannot pass vacuously on a machine without the package — there, the real
``get_bge_m3_model`` returns None before it ever reaches the class.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from faultmaven.config.settings import DeploymentMode, FaultMavenSettings
from faultmaven.infrastructure import model_cache as model_cache_module
from faultmaven.infrastructure.llm.cache import SemanticCache
from faultmaven.infrastructure.model_cache import BGE_M3_MODEL_ID, model_cache
from faultmaven.modules.knowledge.domain.services.ingestion import KnowledgeIngester

pytestmark = [pytest.mark.unit]


@pytest.fixture
def loader(monkeypatch):
    """A live-but-instrumented loader: an empty cache and a stand-in class.

    Returns the ``SentenceTransformer`` mock — ``loader.called`` is "the process
    paid for a model load". The cache is emptied per test so a model loaded by
    an earlier test cannot make ``peek`` answer for this one.
    """
    monkeypatch.setattr(model_cache_module, "SENTENCE_TRANSFORMERS_AVAILABLE", True)
    monkeypatch.setattr(model_cache, "_models", {})
    monkeypatch.setattr(model_cache, "_load_info", {})
    fake_class = MagicMock(name="SentenceTransformer")
    monkeypatch.setattr(model_cache_module, "SentenceTransformer", fake_class)
    return fake_class


def _standalone_settings(tmp_path) -> FaultMavenSettings:
    """REAL settings — a stand-in's ``is_cloud`` is truthy in both modes, which
    would send the ingester down the wrong ChromaDB branch."""
    settings = FaultMavenSettings(_env_file=None)
    settings.deployment_mode = DeploymentMode.STANDALONE
    settings.database.chromadb_url = ""
    settings.database.chromadb_host = "localhost"
    settings.database.vector_storage_type = "chromadb"
    settings.database.chromadb_kb_persist_dir = str(tmp_path / "chroma-kb")
    settings.server.skip_service_checks = False
    return settings


# --------------------------------------------------------------------------- #
# The two constructors that used to load
# --------------------------------------------------------------------------- #


def test_knowledge_ingester_construction_loads_no_model(loader, tmp_path):
    """The OOM path: the container builds one of these in every process."""
    KnowledgeIngester(settings=_standalone_settings(tmp_path))

    loader.assert_not_called()


def test_semantic_cache_construction_loads_no_model(loader):
    """``LLMRouter.__init__`` builds a SemanticCache — so this runs everywhere
    the LLM router is wired, cleanup jobs included."""
    SemanticCache()

    loader.assert_not_called()


def test_semantic_cache_encoder_access_loads_no_model(loader):
    """Moving the load from ``__init__`` to a property must not merely defer it.

    A property that loaded on first touch would trade an OOM for a 60–120s
    synchronous load on the request path — blocking the event loop and tripping
    the liveness probe. Absent means absent: degrade to exact-key matching.
    """
    cache = SemanticCache()

    assert cache.encoder is None
    loader.assert_not_called()


# --------------------------------------------------------------------------- #
# ...and the gate can pass: a resident model is still used
# --------------------------------------------------------------------------- #


def test_semantic_cache_uses_the_model_once_resident(loader, monkeypatch):
    """Proves the property reads the cache rather than being hardwired to None
    — otherwise semantic similarity would be silently dead everywhere."""
    resident = object()
    monkeypatch.setitem(model_cache._models, BGE_M3_MODEL_ID, resident)
    cache = SemanticCache()

    assert cache.encoder is resident
    loader.assert_not_called()


# --------------------------------------------------------------------------- #
# The accessor contract: peek observes, get decides
# --------------------------------------------------------------------------- #


def test_peek_returns_none_without_loading(loader):
    assert model_cache.peek_bge_m3_model() is None
    loader.assert_not_called()


def test_peek_returns_the_resident_model(loader, monkeypatch):
    resident = object()
    monkeypatch.setitem(model_cache._models, BGE_M3_MODEL_ID, resident)

    assert model_cache.peek_bge_m3_model() is resident
    loader.assert_not_called()


def test_get_still_loads(loader):
    """The contrast that gives ``peek`` its meaning: the explicit accessor —
    used by the lifespan preload and by ``aembed_*`` on first use — still loads.
    If this ever stopped loading, the 'no eager load' assertions above would
    pass for the wrong reason."""
    assert model_cache.get_bge_m3_model() is loader.return_value

    loader.assert_called_once_with(BGE_M3_MODEL_ID)


# --------------------------------------------------------------------------- #
# The other half of the memory bill: import weight
# --------------------------------------------------------------------------- #


def test_importing_the_container_does_not_import_torch():
    """Not loading the model is not enough to fit a 512Mi pod.

    ``model_cache`` used to do a module-scope ``from sentence_transformers
    import SentenceTransformer``, which drags torch in behind it — ~690 MiB of
    RSS in every process that imports the module, model or no model. Deferring
    only the *load* left the cleanup jobs still OOMKilled; the import has to be
    deferred too, so both halves are pinned.

    Runs in a subprocess because this test session has almost certainly
    imported torch already, which would make an in-process ``sys.modules``
    assertion pass or fail for reasons that have nothing to do with the fix.
    """
    probe = (
        "import sys;"
        "import faultmaven.container;"
        "import faultmaven.infrastructure.model_cache;"
        "print(','.join(m for m in ('sentence_transformers','torch') "
        "if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[3],
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        f"importing the container pulled in {result.stdout.strip()} — the "
        "embedding stack must stay behind a lazy import (#868)"
    )
