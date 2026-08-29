"""Constructing a service must never load the embedding model (#868).

Every process that builds the DI container — the API pod *and* the cleanup
CronJobs — used to pull the ~1.3Gi BGE-M3 SentenceTransformer during
construction, because two constructors called ``get_bge_m3_model()``: the LLM
response cache (built by ``LLMRouter``) and ``KnowledgeIngester``. A
``storage_cleanup`` job that embeds nothing was OOMKilled at its 512Mi limit,
which is how every app CronJob failed silently for 112 days (infra#131).

The invariant these tests pin: **construction is not embedding.** Whether the
model is resident is decided by the documented policy (``LAZY_LOAD_ML_MODELS`` /
``PRELOAD_MODELS``, applied in the web lifespan) or by the first real
``aembed_*`` call — never as a side effect of building an object.

For the LLM response cache the guarantee is now stronger than "does not load".
Its semantic-matching branch was deleted in #940 — the embed it did was a bare
synchronous ``encode`` on the event loop, and near-match serving is unsound for
investigation turns — so the module has no handle on the embedding stack at all.
The tests below pin that absence structurally as well as behaviourally, because
a module that cannot reach the loader cannot regress into calling it.

The probe is ``SentenceTransformer`` itself rather than ``get_bge_m3_model``:
that catches *any* path to a load, including one added later through a third
constructor. ``SENTENCE_TRANSFORMERS_AVAILABLE`` is forced True so the assertion
cannot pass vacuously on a machine without the package — there, the real
``get_bge_m3_model`` returns None before it ever reaches the class.
"""

import ast
import importlib.util
import inspect
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from faultmaven.config.settings import DeploymentMode, FaultMavenSettings
from faultmaven.infrastructure import model_cache as model_cache_module
from faultmaven.infrastructure.llm import cache as llm_cache_module
from faultmaven.infrastructure.llm.cache import LLMResponseCache
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


def test_response_cache_construction_loads_no_model(loader):
    """``LLMRouter.__init__`` builds an LLMResponseCache — so this runs
    everywhere the LLM router is wired, cleanup jobs included."""
    LLMResponseCache()

    loader.assert_not_called()


def test_exercising_the_response_cache_loads_no_model(loader):
    """Not loading during construction is worth nothing if the first *use*
    loads instead — that trades an OOM in the CronJobs for a 60–120s
    synchronous load on the request path, blocking the event loop and tripping
    the liveness probe. Both halves of the cache's public surface are driven
    here: a store, a hit, and a miss.
    """
    from faultmaven.infrastructure.llm.providers import LLMResponse

    cache = LLMResponseCache()
    response = LLMResponse(
        content="restart the kubelet",
        confidence=0.9,
        provider="openai",
        model="gpt-5.4-mini",
        tokens_used=42,
        response_time_ms=10,
    )
    cache.store("why is node-3 NotReady?", "gpt-5.4-mini", response, case_id="c-1")
    assert cache.check("why is node-3 NotReady?", "gpt-5.4-mini", case_id="c-1")
    assert cache.check("something else entirely", "gpt-5.4-mini", case_id="c-1") is None

    loader.assert_not_called()


def test_response_cache_module_holds_no_handle_on_the_model_cache():
    """After #940 removed semantic matching, ``llm/cache.py`` has no route to
    the loader at all — no module object, no singleton, no alias of either
    bound anywhere in its namespace. Swept over the whole namespace by identity
    rather than by import name, so any *module-level* rebinding fails here
    whatever it is called.

    A module-level sweep is necessarily blind to a function-body import, which
    binds nothing until it runs; the source scan below covers that half.
    """
    reachable = {id(model_cache_module), id(model_cache_module.model_cache)}
    leaked = sorted(
        name for name, value in vars(llm_cache_module).items() if id(value) in reachable
    )

    assert leaked == [], (
        f"llm/cache.py exposes the embedding stack via {leaked} — the response "
        "cache must not be able to reach a model load (#868, #940)"
    )


def _executable_source(module) -> str:
    """The module's code with docstrings dropped, lowercased.

    Docstrings are excluded because ``llm/cache.py``'s header *narrates* the
    removed BGE-M3 branch — that history is the reason the guarantee exists and
    must stay readable. String literals inside code are kept, so a lazy
    ``importlib.import_module("...model_cache")`` still shows up.
    """
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body.pop(0)
            if not node.body:
                node.body.append(ast.Pass())
    return ast.unparse(tree).lower()


@pytest.mark.parametrize("token", ["model_cache", "sentencetransformer", "bge"])
def test_response_cache_code_never_names_the_embedding_stack(token):
    """The evasion the namespace sweep above cannot see: an import inside
    ``check`` or ``store`` binds nothing at module level, so ``vars()`` stays
    clean while the request path loads a 1.3Gi model on the event loop — the
    exact #868/#940 failure. Reading the code catches it wherever it is
    written, function bodies and lazy accessors included.

    Scoped to these three tokens on purpose: the embedding stack's module, its
    class, and its model family — every spelling that reaches a load today.
    A tripwire on the known route, not a proof that no route exists; the
    identity-level check is the namespace sweep above.
    """
    assert token not in _executable_source(llm_cache_module), (
        f"llm/cache.py names {token!r} in code — the response cache must have "
        "no route to an embedding model load, deferred ones included (#868, #940)"
    )


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

    Imports the modules actually under review, NOT just the container:
    ``import faultmaven.container`` pulls in ~50 modules and none of these
    three, so a container-only probe would sit green while a module-scope
    ``import sentence_transformers`` was added straight back into
    ``llm/cache.py`` — one of the two constructors that caused the OOM.

    Asserts provenance too. The venv installs faultmaven editable against the
    main checkout, so an interpreter that ignores cwd (``PYTHONSAFEPATH`` /
    ``-P``, both inherited by subprocess) would import THAT tree and report its
    behaviour as if it were this branch's. Passing an explicit ``PYTHONPATH``
    and checking ``__file__`` makes the probe say which tree it measured.
    """
    repo_root = Path(__file__).resolve().parents[3]
    probe = (
        "import sys;"
        "import faultmaven.container;"
        "import faultmaven.infrastructure.model_cache as mc;"
        "import faultmaven.infrastructure.llm.cache;"
        "import faultmaven.modules.knowledge.domain.services.ingestion;"
        "print(mc.__file__);"
        "print(','.join(m for m in ('sentence_transformers','torch') "
        "if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=repo_root,
        env={**os.environ, "PYTHONPATH": str(repo_root)},
    )

    assert result.returncode == 0, result.stderr
    # Second line is empty in the passing case, which strip() would eat.
    lines = result.stdout.splitlines()
    measured_file = lines[0]
    heavy = lines[1] if len(lines) > 1 else ""

    assert Path(measured_file).resolve().is_relative_to(repo_root), (
        f"probe imported {measured_file}, which is outside {repo_root} — it "
        "measured a different checkout, so its verdict says nothing about "
        "this working tree"
    )
    assert heavy.strip() == "", (
        f"importing the reviewed modules pulled in {heavy.strip()} — the "
        "embedding stack must stay behind a lazy import (#868)"
    )


# --------------------------------------------------------------------------- #
# Deferring the import must not misreport the stack as absent
# --------------------------------------------------------------------------- #


def test_availability_survives_a_spec_less_stand_in(monkeypatch):
    """``find_spec`` raises ValueError for a module that is in ``sys.modules``
    with ``__spec__ is None`` — the exact shape of the stand-in
    ``tests/conftest.py`` installs. Answering 'absent' there would mark the
    embedding stack unavailable for the whole suite and make the real-model
    path silently untestable, so an already-present module has to win first."""
    stub = types.ModuleType("sentence_transformers")
    # As conftest builds it (conftest.py sets ``_mock_st.SentenceTransformer``).
    # This used to be a bare ModuleType, which is NOT that shape: a module
    # exposing no SentenceTransformer is exactly the namespace shadow of #1233
    # and is genuinely not obtainable, so the sys.modules branch now
    # discriminates on the attribute rather than returning True for anything
    # present. The trap this test guards — never asking find_spec first — is
    # unchanged, and the real stand-in is covered by
    # test_the_conftest_stand_in_still_reads_as_obtainable.
    stub.SentenceTransformer = type("SentenceTransformer", (), {})
    assert stub.__spec__ is None  # the trap this guards
    monkeypatch.setitem(sys.modules, "sentence_transformers", stub)

    # The naive probe really does blow up on this input...
    with pytest.raises(ValueError):
        importlib.util.find_spec("sentence_transformers")

    # ...and the real one answers correctly anyway.
    assert model_cache_module._sentence_transformers_obtainable() is True


def test_availability_is_true_under_this_test_suite():
    """The suite runs with conftest's stand-in installed; if the flag reads
    False here, every assertion about the real-model path is vacuous."""
    assert model_cache_module.SENTENCE_TRANSFORMERS_AVAILABLE is True


def test_availability_reports_absent_when_nothing_provides_it(monkeypatch):
    """...and the flag can still say False, or it means nothing."""
    monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

    assert model_cache_module._sentence_transformers_obtainable() is False


# --------------------------------------------------------------------------- #
# A leftover directory is not an installed package (#1233)
# --------------------------------------------------------------------------- #


def _probe_spec(tmp_path, monkeypatch, name, init_py=None):
    """A ModuleSpec produced by real CPython import machinery, not hand-built.

    A namespace-package spec is the shape under test, and fabricating one
    (``ModuleSpec(name, loader=None, origin=None)``) would assert against this
    test's idea of a namespace package rather than the import system's. So the
    directory is real: empty for a namespace package, or carrying an
    ``__init__.py`` for a regular one.

    Asked under a unique name because the installed ``sentence_transformers``
    would otherwise win the namespace scan — the trap that makes an empty dir on
    ``sys.path`` measure the real package instead (#1231).
    """
    package = tmp_path / name
    package.mkdir()
    if init_py is not None:
        (package / "__init__.py").write_text(init_py)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    spec = importlib.util.find_spec(name)
    monkeypatch.delitem(sys.modules, name, raising=False)
    return spec


@pytest.mark.parametrize(
    "init_py, expect_obtainable",
    [
        pytest.param(None, False, id="namespace-package-shadow"),
        pytest.param("", True, id="real-package-empty-init"),
        pytest.param("VALUE = 1\n", True, id="real-package"),
    ],
)
def test_a_leftover_directory_is_not_an_installed_package(
    tmp_path, monkeypatch, init_py, expect_obtainable
):
    """An empty ``sentence_transformers/`` must not read as installed.

    pip and uv remove a package's files on uninstall but leave its directories,
    and PEP 420 resolves the leftover tree to a namespace package: ``find_spec``
    returns a spec, ``spec.origin`` is None, and nothing raises — so the
    ``except (ImportError, ValueError)`` around it cannot cover this, however
    its comment used to read. The venv this repo is developed in was in exactly
    that state for ``opik`` (#1231).

    The cost of the wrong answer is not just a wrong flag — see
    ``test_an_unavailable_stack_never_reaches_the_torch_import``.
    """
    suffix = "real" if init_py is not None else "ns"
    spec = _probe_spec(tmp_path, monkeypatch, f"_fm_st_probe_{suffix}", init_py)
    # What the parametrization claims about the fixture, before relying on it.
    # ``spec is not None`` first: a probe that failed to resolve must report
    # that, not an AttributeError saying nothing about the fixture being at
    # fault.
    assert spec is not None, "probe package did not resolve — the fixture is broken"
    assert (spec.origin is not None) is expect_obtainable

    # sys.modules wins first (conftest installs a stand-in), so the find_spec
    # branch is unreachable until that is cleared.
    monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: spec)

    assert model_cache_module._sentence_transformers_obtainable() is expect_obtainable


@pytest.mark.parametrize(
    "attrs, expect_obtainable",
    [
        pytest.param({}, False, id="imported-namespace-shadow"),
        pytest.param(
            {"SentenceTransformer": object}, True, id="imported-real-or-stand-in"
        ),
    ],
)
def test_an_already_imported_module_is_not_automatically_obtainable(
    monkeypatch, attrs, expect_obtainable
):
    """The ``sys.modules`` branch must discriminate too.

    Importing a namespace shadow SUCCEEDS — it installs a module object with
    ``__file__ is None`` that exposes nothing. So "present in sys.modules" is
    not "obtainable": any earlier importer in the process (a dependency probe,
    a worker, a bare ``try: import sentence_transformers``) would hand this
    branch the very state the find_spec branch exists to reject, and the flag
    would read True again — the pre-fix behaviour, torch import included.

    ``__file__`` cannot be the discriminator here: conftest's stand-in is a bare
    ``ModuleType`` and has none. The attribute the caller actually needs can be,
    and it is free because the module is already imported (no #868 cost).
    """
    module = types.ModuleType("sentence_transformers")
    for name, value in attrs.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)

    assert model_cache_module._sentence_transformers_obtainable() is expect_obtainable


def test_the_conftest_stand_in_still_reads_as_obtainable():
    """...and the real stand-in, not a reconstruction of it, still passes.

    The sys.modules branch exists so conftest's double wins; a discriminator
    that rejected it would mark the embedding stack absent for the whole
    session and make every real-model assertion vacuous."""
    assert sys.modules["sentence_transformers"].__spec__ is None  # the trap
    assert model_cache_module._sentence_transformers_obtainable() is True


def test_an_unavailable_stack_never_reaches_the_torch_import(monkeypatch):
    """A False flag must stop short of ``configure_inference_threads()``.

    That function imports torch — the ~690 MiB #868 exists to keep out of the
    cleanup CronJobs — so the memory win this whole module is shaped around
    lives at that call, not at the flag. Without this, moving the call above the
    availability check would leave every 512Mi CronJob importing torch again
    with the suite still green.
    """
    called = []
    monkeypatch.setattr(
        model_cache_module, "configure_inference_threads", lambda: called.append(1)
    )
    monkeypatch.setattr(model_cache_module, "SENTENCE_TRANSFORMERS_AVAILABLE", False)

    cache = model_cache_module.ModelCache()
    cache.clear_cache()

    assert cache.get_bge_m3_model(triggered_by="lazy") is None
    assert called == [], "torch import was reached despite an unavailable stack"


def test_a_wrong_true_flag_still_never_reaches_the_torch_import(monkeypatch):
    """And ordering makes the whole CLASS of wrong-True flags harmless.

    The flag can be True and the package still unimportable — a shadow already
    in sys.modules, a half-removed tree whose __init__.py survived, a version
    conflict raising inside the package. Resolving the class BEFORE configuring
    threads means those fail without paying for torch, instead of each having to
    be enumerated and guarded at the flag.
    """
    called = []
    monkeypatch.setattr(
        model_cache_module, "configure_inference_threads", lambda: called.append(1)
    )
    monkeypatch.setattr(model_cache_module, "SENTENCE_TRANSFORMERS_AVAILABLE", True)

    def _unimportable():
        raise ImportError("cannot import name 'SentenceTransformer'")

    monkeypatch.setattr(
        model_cache_module, "_sentence_transformer_class", _unimportable
    )

    cache = model_cache_module.ModelCache()
    cache.clear_cache()

    assert cache.get_bge_m3_model(triggered_by="lazy") is None
    assert called == [], "torch import was reached before the package proved importable"


def test_thread_env_is_pinned_before_the_torch_pulling_import(monkeypatch):
    """The OMP/MKL/OpenBLAS knobs must be set BEFORE sentence_transformers.

    Those variables are read by the native libraries when they initialise their
    pools, which happens at torch import — and sentence_transformers pulls
    torch. Setting them afterwards is a no-op. Measured on a 48-core host:

        env set, then import torch  -> torch.get_num_threads() == 2
        import torch, then env set  -> torch.get_num_threads() == 24

    #1253 reordered this block so the class import came first (so that a
    wrong-True flag could not reach `import torch`), which silently moved the
    env knobs after the pools they configure. Both properties are required, so
    the env half is split out and runs first; this pins the ordering.
    """
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        monkeypatch.delenv(var, raising=False)

    seen = {}

    def _record_class():
        seen["omp_at_import"] = os.environ.get("OMP_NUM_THREADS")
        return lambda model_key: object()

    monkeypatch.setattr(
        model_cache_module, "_sentence_transformer_class", _record_class
    )
    monkeypatch.setattr(model_cache_module, "SENTENCE_TRANSFORMERS_AVAILABLE", True)

    cache = model_cache_module.ModelCache()
    cache.clear_cache()
    assert cache.get_bge_m3_model(triggered_by="lazy") is not None

    assert seen["omp_at_import"] is not None, (
        "OMP_NUM_THREADS was unset when sentence_transformers was imported — "
        "the native thread pools initialise there, so pinning after that point "
        "does nothing"
    )


def test_pinning_the_env_imports_nothing_heavy(monkeypatch):
    """pin_thread_env() runs before the availability gate is proven, so it must
    not be a way back into the ~690 MiB #868 keeps out."""
    import sys

    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        monkeypatch.delenv(var, raising=False)
    before = set(sys.modules)

    threads = model_cache_module.pin_thread_env()

    assert threads >= 1
    assert os.environ["OMP_NUM_THREADS"] == str(threads)
    newly_imported = {m.split(".")[0] for m in set(sys.modules) - before}
    assert not (
        newly_imported & {"torch", "sentence_transformers", "transformers"}
    ), f"pin_thread_env pulled in {newly_imported}"
