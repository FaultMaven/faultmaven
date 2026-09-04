import os
import sys
from unittest.mock import Mock

# CRITICAL: Aggressively clear LLM credentials from os.environ at the VERY START of collection.
# This prevents early-imported modules from capturing real credentials during Pytest collection.
LLM_VARS_TO_WIPE = [
    "CHAT_PROVIDER",
    "FIREWORKS_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GROQ_API_KEY",
    "HUGGINGFACE_API_KEY",
    "COHERE_API_KEY",
    "OPENROUTER_API_KEY",
    "STRICT_PROVIDER_MODE",
]
for var in LLM_VARS_TO_WIPE:
    os.environ.pop(var, None)

# CRITICAL: Prevent .env file loading in tests to avoid environment contamination
# This must happen before ANY other imports, as some modules load settings at import time.
# Pydantic BaseSettings uses dotenv.dotenv_values() internally (not load_dotenv),
# so we patch BOTH to fully prevent .env leakage into nested settings models.
try:
    import dotenv

    # Patch dotenv globally to be a no-op during tests
    dotenv.load_dotenv = Mock(return_value=None)
    dotenv.dotenv_values = Mock(return_value={})

    # Also patch DotEnvSettingsSource directly in case pydantic_settings was already imported
    try:
        import pydantic_settings.sources.providers.dotenv as pydantic_dotenv

        pydantic_dotenv.dotenv_values = dotenv.dotenv_values
        # To be absolutely sure, patch the method that reads the files
        pydantic_dotenv.DotEnvSettingsSource._read_env_files = lambda self: {}
    except (ImportError, AttributeError):
        pass
except ImportError:
    pass  # dotenv not installed, which is fine for tests

import importlib.machinery
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock

# --------------------------------------------------------------------------- #
# Harness stand-ins (#942)
#
# The harness substitutes heavy third-party dependencies so the suite does not
# import ~690 MiB of torch (#868) or re-register TORCH_LIBRARY. Two rules keep a
# substitution from silently becoming the thing under test:
#
#   1. A stand-in must be indistinguishable from the real module to a NON-
#      IMPORTING availability probe. A hand-built module object has
#      ``__spec__ is None``, and ``importlib.util.find_spec(name)`` RAISES
#      ``ValueError`` for such a name rather than returning a spec. So
#      ``find_spec`` -- the correct way to ask "is this installed" without
#      paying the import -- answers differently under the harness than in
#      production, and a green suite is not evidence the probe works (#942).
#      ``_install_stand_in`` attaches a real ``ModuleSpec`` so it does not.
#
#   2. A stand-in may only stand in for something ABSENT or deliberately
#      excluded -- never for a first-party ``faultmaven.*`` module, which is the
#      thing under test. Shadowing one makes every test that appears to exercise
#      it exercise a ``Mock`` instead.
#
# ``HARNESS_STAND_INS`` records what was actually substituted;
# ``tests/unit/test_harness_stand_ins.py`` pins both rules against it.
# --------------------------------------------------------------------------- #

HARNESS_STAND_INS: dict[str, object] = {}


def _install_stand_in(name: str, module):
    """Install ``module`` as the harness stand-in for ``name``.

    No-ops when ``name`` is already in ``sys.modules`` -- the real module, or an
    earlier stand-in, wins and nothing is recorded, so the registry names only
    what this harness actually substituted.

    Refuses a first-party ``faultmaven.*`` name outright. That is rule 2 above,
    enforced where the mistake is made rather than only in a test, because the
    failure mode is silent: the shadowed module still imports, still exposes the
    expected attribute names, and the tests still pass.
    """
    if name == "faultmaven" or name.startswith("faultmaven."):
        raise RuntimeError(
            f"refusing to install a harness stand-in for {name!r}: the harness "
            "may substitute an absent or deliberately excluded third-party "
            "dependency, never a first-party module, which is the thing under "
            "test (#942). Patch the collaborator in the test instead."
        )
    if name in sys.modules:
        return sys.modules[name]

    # A real ModuleSpec, so find_spec(name) returns rather than raising
    # ValueError. loader=None matches what a stand-in truthfully is: located,
    # but not loadable from disk.
    #
    # This mutates a caller-owned object, so installing ONE object under two
    # names would silently relabel the first entry's spec and make find_spec
    # answer with the wrong name. Every call site passes a fresh object; this
    # refuses rather than letting a future one re-use one by accident.
    existing = getattr(module, "__spec__", None)
    if existing is not None and getattr(existing, "name", name) != name:
        raise RuntimeError(
            f"refusing to install {existing.name!r} a second time as {name!r}: "
            "one module object cannot carry two names, and re-labelling its "
            "spec would make find_spec answer with the wrong one. Build a "
            "separate stand-in for each name."
        )
    module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
    sys.modules[name] = module
    HARNESS_STAND_INS[name] = module
    return module


# CRITICAL: Mock heavy ML dependencies FIRST to prevent PyTorch TORCH_LIBRARY errors
# This must happen before ANY imports that could trigger torch/transformers/sentence-transformers
# The PyTorch TORCH_LIBRARY registration error occurs when torch is imported multiple times

# Mock torch to prevent TORCH_LIBRARY registration errors
if "torch" not in sys.modules:

    class _MockTorchModule(ModuleType):
        """Comprehensive mock for torch module."""

        __version__ = "2.0.0"

        def __getattr__(self, name):
            # Return mock for any torch attribute
            if name in (
                "nn",
                "optim",
                "cuda",
                "jit",
                "onnx",
                "autograd",
                "utils",
                "distributed",
            ):
                # Return a module-like mock for submodules
                mock_submodule = ModuleType(f"torch.{name}")
                mock_submodule.__getattr__ = lambda self, n: Mock()
                return mock_submodule
            return Mock()

    _mock_torch = _MockTorchModule("torch")
    _mock_torch.__path__ = (
        []
    )  # Must be iterable; import system iterates __path__ for subpackages
    _mock_torch.Tensor = type("Tensor", (), {})
    _mock_torch.device = Mock
    _mock_torch.dtype = Mock
    _install_stand_in("torch", _mock_torch)
    _install_stand_in("torch.nn", ModuleType("torch.nn"))
    _install_stand_in("torch.optim", ModuleType("torch.optim"))
    _install_stand_in("torch.cuda", ModuleType("torch.cuda"))

# Mock transformers to prevent heavy imports
if "transformers" not in sys.modules:
    _mock_transformers = ModuleType("transformers")
    _mock_transformers.__version__ = "4.36.0"
    _mock_transformers.AutoModel = Mock
    _mock_transformers.AutoTokenizer = Mock
    _mock_transformers.PreTrainedModel = type("PreTrainedModel", (), {})
    _install_stand_in("transformers", _mock_transformers)
    _install_stand_in("transformers.utils", ModuleType("transformers.utils"))

# Mock sentence_transformers to prevent model loading
if "sentence_transformers" not in sys.modules:

    class _MockSentenceTransformer:
        """Mock SentenceTransformer class."""

        def __init__(self, model_name_or_path, *args, **kwargs):
            self.model_name_or_path = model_name_or_path

        def encode(self, sentences, *args, **kwargs):
            # Return deterministic fake embeddings based on text hash
            import hashlib

            import numpy as np

            def get_deterministic_vector(text):
                # Use a simple hash to seed random for deterministic output
                seed = int(hashlib.md5(text.encode("utf-8")).hexdigest()[:8], 16)
                # Save current state
                state = np.random.get_state()
                try:
                    np.random.seed(seed)
                    res = np.random.rand(384).astype(np.float32)
                    return res
                finally:
                    # Restore state so we don't affect global random state
                    np.random.set_state(state)

            if isinstance(sentences, str):
                return get_deterministic_vector(sentences)
            return np.array(
                [get_deterministic_vector(s) for s in sentences], dtype=np.float32
            )

    _mock_st = ModuleType("sentence_transformers")
    _mock_st.SentenceTransformer = _MockSentenceTransformer
    _mock_st.__version__ = "2.2.2"
    _install_stand_in("sentence_transformers", _mock_st)

# CRITICAL: Mock _ctypes FIRST, before any other imports
# This must happen before any module tries to import ctypes (e.g., protobuf, numpy, chromadb)
# Python installations built without libffi support will be missing _ctypes
# Note: We only mock _ctypes, not ctypes itself, to avoid breaking numpy
try:
    import _ctypes
except ImportError:
    # Create a comprehensive mock for _ctypes with dynamic attribute access
    # This allows protobuf/ctypes to import without errors
    # Python installations built without libffi support will be missing _ctypes
    class _CTypesMock(ModuleType):
        """Dynamic mock for _ctypes that provides any requested attribute."""

        __version__ = "1.1.0"  # Must match ctypes expected version

        # Essential type classes
        Union = type("Union", (), {})
        Structure = type("Structure", (), {})
        Array = type("Array", (), {})
        _Pointer = type("_Pointer", (), {})
        CFuncPtr = type("CFuncPtr", (), {})
        _FuncPtr = type("_FuncPtr", (), {})

        # Constants
        RTLD_LOCAL = 0
        RTLD_GLOBAL = 1
        FUNCFLAG_CDECL = 2
        FUNCFLAG_PYTHONAPI = 4
        FUNCFLAG_USE_ERRNO = 8
        FUNCFLAG_USE_LASTERROR = 16

        # Exception classes
        ArgumentError = type("ArgumentError", (Exception,), {})

        def sizeof(self, obj):
            """Mock sizeof that returns reasonable defaults for common types."""
            # Return reasonable sizes for common ctypes types
            type_name = (
                str(type(obj).__name__) if hasattr(obj, "__name__") else str(type(obj))
            )
            size_map = {
                "c_int": 4,
                "c_long": 8,
                "c_void_p": 8,
                "c_char_p": 8,
                "c_double": 8,
                "c_float": 4,
                "str": 8,  # Pointer size on 64-bit
            }
            # Check if it's a known type or return default pointer size
            for key, size in size_map.items():
                if key in type_name.lower():
                    return size
            return 8  # Default to pointer size on 64-bit systems

        def __getattr__(self, name):
            # Return appropriate values for known functions
            if name == "sizeof":
                return self.sizeof
            elif name in ("addressof", "alignment"):
                return Mock(return_value=0)
            elif name in ("get_errno", "set_errno"):
                return Mock(return_value=0)
            else:
                # Return a Mock callable for any other missing function/attribute
                return Mock(return_value=None)

    _mock_ctypes = _CTypesMock("_ctypes")
    _install_stand_in("_ctypes", _mock_ctypes)

    # Also create a minimal ctypes mock for numpy compatibility
    # numpy.ctypeslib needs ctypes.c_byte, c_short, etc.
    class _CTypesModuleMock(ModuleType):
        """Minimal ctypes module mock for numpy compatibility."""

        def __getattr__(self, name):
            # Return Mock objects for ctypes types (c_int, c_byte, etc.)
            if name.startswith("c_"):
                return type(f"c_{name[2:]}", (), {"_type_": name})
            # Return Mock for functions
            return Mock(return_value=None)

    _mock_ctypes_module = _CTypesModuleMock("ctypes")
    # Add common ctypes types that numpy needs
    for ctype_name in [
        "c_byte",
        "c_short",
        "c_int",
        "c_long",
        "c_longlong",
        "c_ubyte",
        "c_ushort",
        "c_uint",
        "c_ulong",
        "c_ulonglong",
        "c_float",
        "c_double",
        "c_char",
        "c_wchar",
        "c_void_p",
        "c_char_p",
    ]:
        setattr(
            _mock_ctypes_module,
            ctype_name,
            type(ctype_name, (), {"_type_": ctype_name}),
        )
    _install_stand_in("ctypes", _mock_ctypes_module)

# Mock _sqlite3 for Python installations missing this built-in module
# Similar to _ctypes, this can happen when Python is built without sqlite support
try:
    import _sqlite3
except ImportError:
    _mock_sqlite3 = ModuleType("_sqlite3")
    # Add minimal sqlite3 interface
    _mock_sqlite3.connect = Mock(return_value=Mock())
    _mock_sqlite3.version = "3.42.0"  # ChromaDB requires >= 3.35.0
    _mock_sqlite3.version_info = (3, 42, 0)
    _mock_sqlite3.sqlite_version = "3.42.0"
    _mock_sqlite3.sqlite_version_info = (3, 42, 0)
    # Add Row class that sqlite3.dbapi2 needs
    _mock_sqlite3.Row = type("Row", (tuple,), {})
    # Add all sqlite3 exception classes for aiosqlite compatibility
    _mock_sqlite3.Error = type("Error", (Exception,), {})
    _mock_sqlite3.Warning = type("Warning", (Exception,), {})
    _mock_sqlite3.DatabaseError = type("DatabaseError", (_mock_sqlite3.Error,), {})
    _mock_sqlite3.IntegrityError = type(
        "IntegrityError", (_mock_sqlite3.DatabaseError,), {}
    )
    _mock_sqlite3.OperationalError = type(
        "OperationalError", (_mock_sqlite3.DatabaseError,), {}
    )
    _mock_sqlite3.ProgrammingError = type(
        "ProgrammingError", (_mock_sqlite3.DatabaseError,), {}
    )
    _mock_sqlite3.InterfaceError = type("InterfaceError", (_mock_sqlite3.Error,), {})
    _mock_sqlite3.InternalError = type(
        "InternalError", (_mock_sqlite3.DatabaseError,), {}
    )
    _mock_sqlite3.DataError = type("DataError", (_mock_sqlite3.DatabaseError,), {})
    _mock_sqlite3.NotSupportedError = type(
        "NotSupportedError", (_mock_sqlite3.DatabaseError,), {}
    )
    _install_stand_in("_sqlite3", _mock_sqlite3)

    # Also mock sqlite3 module itself to prevent import errors
    # sqlite3.dbapi2 tries to call register_adapter during init, which fails without _sqlite3
    try:
        import sqlite3
    except (ImportError, NameError):
        _mock_sqlite3_module = ModuleType("sqlite3")
        _mock_sqlite3_module.Row = _mock_sqlite3.Row
        # Add Cursor and Connection classes
        _mock_sqlite3_module.Cursor = type("Cursor", (), {})
        _mock_sqlite3_module.Connection = type("Connection", (), {})
        _mock_sqlite3_module.connect = Mock(return_value=Mock())
        _mock_sqlite3_module.version = "3.42.0"  # ChromaDB requires >= 3.35.0
        _mock_sqlite3_module.version_info = (3, 42, 0)
        _mock_sqlite3_module.sqlite_version = "3.42.0"
        _mock_sqlite3_module.sqlite_version_info = (3, 42, 0)
        # Copy all exception classes from _sqlite3
        _mock_sqlite3_module.Error = _mock_sqlite3.Error
        _mock_sqlite3_module.Warning = _mock_sqlite3.Warning
        _mock_sqlite3_module.DatabaseError = _mock_sqlite3.DatabaseError
        _mock_sqlite3_module.IntegrityError = _mock_sqlite3.IntegrityError
        _mock_sqlite3_module.OperationalError = _mock_sqlite3.OperationalError
        _mock_sqlite3_module.ProgrammingError = _mock_sqlite3.ProgrammingError
        _mock_sqlite3_module.InterfaceError = _mock_sqlite3.InterfaceError
        _mock_sqlite3_module.InternalError = _mock_sqlite3.InternalError
        _mock_sqlite3_module.DataError = _mock_sqlite3.DataError
        _mock_sqlite3_module.NotSupportedError = _mock_sqlite3.NotSupportedError
        # Add DB-API 2.0 module attributes that aiosqlite needs
        _mock_sqlite3_module.paramstyle = "qmark"  # Standard SQLite parameter style
        _mock_sqlite3_module.threadsafety = 1
        _mock_sqlite3_module.apilevel = "2.0"
        _mock_sqlite3_module.register_converter = Mock()
        _install_stand_in("sqlite3", _mock_sqlite3_module)


# NOTE: faultmaven.infrastructure.observability.{apm_integration,apm_metrics,
# alerting} used to be stubbed here "to avoid import-side initialization".
# All three exist on disk and have production importers (main.py,
# api/middleware/performance.py), so the stubs made every test that appeared to
# exercise them exercise a SimpleNamespace of lambdas instead -- and one test
# had already grown a hand-rolled `del sys.modules[...]` to reach past them.
# Removed with #942: the harness may substitute what is absent, never a
# first-party module.
#
# What that makes live, stated accurately (an earlier revision of this comment
# claimed "no threads, no I/O and no asyncio tasks", which was false):
#   * `AlertManager.__init__` and `APMIntegration.__init__` both call
#     `get_settings()`, so importing these freezes a settings snapshot for the
#     session.
#   * `alert_manager` and `metrics_collector` are module-level singletons with
#     MUTABLE state, and `PerformanceMiddleware._record_performance_metrics`
#     writes to both on EVERY request. Under the old stub those calls hit a
#     SimpleNamespace lacking the methods and were swallowed, recording nothing.
#   * `alert_manager._schedule_notification` does `asyncio.create_task(...)`,
#     falling back on RuntimeError to `_send_sync_notification`, which itself
#     calls `asyncio.run(...)` (alerting.py:469) -- and that raises inside a
#     running loop.
# The accumulating state is therefore reset between tests by the autouse
# `_reset_observability_singletons` fixture below; without it every TestClient
# request leaves residue and any future assertion on /metrics/* would be
# order-dependent.

"""Shared pytest fixtures and configuration for FaultMaven tests."""

import asyncio
import os
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

# Accumulating state on the observability singletons that #942 un-stubbed. Named
# explicitly rather than reconstructed, and pinned by
# tests/unit/test_harness_stand_ins.py so the list cannot drift into a no-op.
# Configuration set up in __init__ (alert_rules, channel_configs,
# notification_handlers, alert_thresholds) is deliberately NOT reset -- it is
# not per-request residue.
OBSERVABILITY_RESET_FIELDS = {
    "faultmaven.infrastructure.observability.apm_metrics": (
        "metrics_collector",
        (
            "metrics",
            "aggregated_cache",
            "last_aggregation",
            "start_times",
            "dashboard_data",
        ),
    ),
    "faultmaven.infrastructure.observability.alerting": (
        "alert_manager",
        ("active_alerts", "alert_history", "suppressed_rules"),
    ),
}


@pytest.fixture(autouse=True)
def _reset_observability_singletons():
    """Clear per-request residue off the un-stubbed observability singletons.

    `PerformanceMiddleware` writes to `metrics_collector` and calls
    `alert_manager.evaluate_metric` on every request, and both are module-level
    singletons. Before #942 those calls hit a stub and recorded nothing; now
    they accumulate for the whole session, which would make any assertion on
    /metrics/* order-dependent. No test asserts on those endpoints today -- this
    exists so that the first one to do so is not silently coupled to whatever
    ran before it.

    Costs one dict lookup per test when the modules were never imported.
    """
    yield
    for module_name, (singleton_name, fields) in OBSERVABILITY_RESET_FIELDS.items():
        module = sys.modules.get(module_name)
        if module is None:
            continue
        singleton = getattr(module, singleton_name, None)
        if singleton is None:
            continue
        for field in fields:
            container = getattr(singleton, field, None)
            if hasattr(container, "clear"):
                container.clear()


# Session-level fixture to clear .env variables that may have been loaded before tests started
@pytest.fixture(scope="session", autouse=True)
def clean_test_environment():
    """Clear all .env-related environment variables before ANY tests run.

    This prevents contamination from the .env file when tests run as part of
    a large suite where settings may have been loaded by previous tests or
    during application initialization.

    Addresses: Test failures due to pydantic-settings caching real .env values
    """
    import os

    # List of all environment variables from .env that should be cleared for tests
    ENV_VARS_TO_CLEAR = [
        "ENVIRONMENT",
        "DEBUG",
        "HOST",
        "PORT",
        "WORKERS",
        "LOG_LEVEL",
        "INSTANCE_ID",
        "CHAT_PROVIDER",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "FIREWORKS_API_KEY",
        "FIREWORKS_MODEL",
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "GROQ_API_KEY",
        "GROQ_MODEL",
        "HUGGINGFACE_API_KEY",
        "HUGGINGFACE_MODEL",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL",
        "LOCAL_LLM_URL",
        "LOCAL_LLM_MODEL",
        "ENABLE_WEB_SEARCH",
        "SESSION_STORAGE_TYPE",
        "VECTOR_STORAGE_TYPE",
        "USER_STORAGE_TYPE",
        "CASE_STORAGE_TYPE",
        "MAX_UPLOAD_SIZE_MB",
        "CORS_ALLOW_CREDENTIALS",
        "CORS_ALLOW_ORIGINS",
        "TENANT_PROVIDER",  # Add any other vars that affect test isolation
    ]

    # Clear all .env variables
    for var in ENV_VARS_TO_CLEAR:
        os.environ.pop(var, None)

    yield

    # Optionally restore after tests (not needed for CI/CD, but helpful for local dev)
    # If restoration is needed, save original values before clearing


# Mock _ctypes module for Python 3.11 compatibility when libffi is not available
# This is needed for protobuf/chromadb imports that depend on ctypes
if "_ctypes" not in sys.modules:
    _mock_ctypes = ModuleType("_ctypes")

    # Create minimal ctypes interface that protobuf expects
    class _MockPointer:
        pass

    class _MockCData:
        pass

    _mock_ctypes.Union = type("Union", (_MockCData,), {})
    _mock_ctypes.Structure = type("Structure", (_MockCData,), {})
    _mock_ctypes.Array = type("Array", (_MockCData,), {})
    _mock_ctypes._Pointer = _MockPointer
    _mock_ctypes._CData = _MockCData
    _mock_ctypes.POINTER = lambda ctype: type(
        f"LP_{getattr(ctype, '__name__', 'unknown')}", (_MockPointer,), {}
    )
    _mock_ctypes.sizeof = lambda obj: 8
    _mock_ctypes.addressof = Mock(return_value=0)
    _mock_ctypes.byref = Mock()
    _mock_ctypes.cast = Mock()
    _mock_ctypes.get_errno = Mock(return_value=0)
    _mock_ctypes.set_errno = Mock()
    _mock_ctypes.ArgumentError = Exception
    _mock_ctypes.RTLD_GLOBAL = 256
    _mock_ctypes.RTLD_LOCAL = 0
    _install_stand_in("_ctypes", _mock_ctypes)

# Mock ctypes module itself
if "ctypes" not in sys.modules:
    _mock_ctypes_module = ModuleType("ctypes")
    # Add all common ctypes that protobuf/numpy expect
    for ctype_name in [
        "c_byte",
        "c_short",
        "c_int",
        "c_long",
        "c_longlong",
        "c_ubyte",
        "c_ushort",
        "c_uint",
        "c_ulong",
        "c_ulonglong",
        "c_float",
        "c_double",
        "c_longdouble",
        "c_char",
        "c_wchar",
        "c_void_p",
        "c_char_p",
        "c_wchar_p",
        "c_size_t",
        "c_bool",
        "c_int8",
        "c_int16",
        "c_int32",
        "c_int64",
        "c_uint8",
        "c_uint16",
        "c_uint32",
        "c_uint64",
    ]:
        setattr(
            _mock_ctypes_module,
            ctype_name,
            type(ctype_name, (), {"__name__": ctype_name}),
        )
    _mock_ctypes_module.Union = Mock()
    _mock_ctypes_module.Structure = Mock()
    _mock_ctypes_module.Array = Mock()
    _mock_ctypes_module.POINTER = Mock()
    _mock_ctypes_module.sizeof = Mock(return_value=8)
    _mock_ctypes_module.addressof = Mock(return_value=0)
    _mock_ctypes_module.byref = Mock()
    _mock_ctypes_module.cast = Mock()
    _mock_ctypes_module.get_errno = Mock(return_value=0)
    _mock_ctypes_module.set_errno = Mock()
    _mock_ctypes_module.ArgumentError = Exception
    _mock_ctypes_module.RTLD_GLOBAL = 256
    _mock_ctypes_module.RTLD_LOCAL = 0
    # Add DLL loading functions that some libraries need
    _mock_ctypes_module.CDLL = Mock()
    _mock_ctypes_module.PyDLL = Mock()
    _mock_ctypes_module.LoadLibrary = Mock()
    _mock_ctypes_module.pythonapi = Mock()
    _install_stand_in("ctypes", _mock_ctypes_module)

# Stub heavy dependencies to avoid import issues in tests
# These stubs prevent importing sklearn, chromadb, pypdf, etc.
# sklearn must have __spec__ attribute for transformers compatibility
import types

if "sklearn" not in sys.modules:
    # The spec here predates #942 -- transformers probes for sklearn and a
    # spec-less stand-in broke it. _install_stand_in generalises that one-off
    # to every stand-in, and upgrades the hand-rolled SimpleNamespace to a real
    # ModuleSpec (find_spec's contract says it returns one).
    _install_stand_in("sklearn", types.ModuleType("sklearn"))
    _install_stand_in("sklearn.ensemble", SimpleNamespace(IsolationForest=Mock))
    _install_stand_in("sklearn.preprocessing", SimpleNamespace(StandardScaler=Mock))
# NOTE: chromadb stub removed - tests need real ChromaDB
# If chromadb is not installed, tests using it will fail as expected
# NOTE: pypdf stub removed with #942. pypdf is a real, installed dependency;
# standing an empty SimpleNamespace in front of it made `from pypdf import
# PdfReader` fail and every PYPDF_AVAILABLE-style probe read False for the
# whole session, so the suite could not be evidence that the PDF path -- or a
# security bump to it -- works.

# Mock _ctypes and ctypes for Python installations missing this built-in module
# This can happen when Python is built without libffi support
# We need to mock both _ctypes and ctypes before any imports try to use them
try:
    import _ctypes
except ImportError:
    # Create a comprehensive mock for _ctypes
    _mock_ctypes = types.ModuleType("_ctypes")
    for attr in [
        "Union",
        "Structure",
        "Array",
        "_Pointer",
        "CFuncPtr",
        "POINTER",
        "c_int",
        "c_void_p",
        "c_char_p",
        "c_long",
        "c_ulong",
        "c_double",
    ]:
        setattr(_mock_ctypes, attr, Mock)
    _install_stand_in("_ctypes", _mock_ctypes)

# Also mock ctypes module itself to prevent import errors
try:
    import ctypes
except (ImportError, AttributeError):
    _mock_ctypes_module = types.ModuleType("ctypes")
    for attr in [
        "Union",
        "Structure",
        "Array",
        "POINTER",
        "c_int",
        "c_void_p",
        "c_char_p",
        "c_long",
        "c_ulong",
        "c_double",
        "CDLL",
        "Structure",
    ]:
        setattr(_mock_ctypes_module, attr, Mock)
    _install_stand_in("ctypes", _mock_ctypes_module)

# NOTE: stubs for faultmaven.core.knowledge.ingestion, faultmaven.tools
# .web_search and faultmaven.core.processing.log_analyzer were removed with
# #942. The first two are fossils of a layout that moved -- neither
# faultmaven/core/knowledge/ nor faultmaven/tools/ exists, and nothing in the
# tree imports either dotted name -- so they stood in for nothing. The third
# names a real 1780-line module with two production importers, which the stub
# replaced with a Mock for the whole session.

# Lazy import tools that may require _ctypes (via chromadb/protobuf) or have version issues
# These will be imported only when needed, not at module level
# Catch all exceptions since the import chain can fail at many levels (torch, transformers, langchain, etc.)
try:
    from faultmaven.modules.agent.tools.web_search import WebSearchTool
except Exception as e:
    # If import fails for any reason (ctypes, langchain version, torch, etc.), create mock versions
    # This allows tests to run even if these heavy dependencies have issues
    WebSearchTool = Mock

# from faultmaven.modules.preprocessing.classifier import DataClassifier  # May need heavy deps
# from faultmaven.core.processing.log_analyzer import LogProcessor
# Conditional import for LLMRouter (may import torch/transformers which can have issues)
try:
    from faultmaven.infrastructure.llm.router import LLMRouter
except Exception:
    # If import fails (torch issues, etc.), create a mock
    LLMRouter = Mock
from faultmaven.infrastructure.security.redaction import DataSanitizer
from faultmaven.models import DataType, SessionContext
from faultmaven.models.common import AgentStateEnum as AgentState

# SessionManager has been replaced by SessionService
# from faultmaven.session_management import SessionManager


def create_agent_state_dict(status=None, case_context=None, current_phase="initial"):
    """Helper to create agent state dictionary from enum status"""
    return {
        "status": status or AgentState.IDLE,
        "case_context": case_context or {},
        "current_phase": current_phase,
        "findings": [],
        "recommendations": [],
        "confidence_score": 0.0,
        "tools_used": [],
        "awaiting_user_input": False,
        "user_feedback": "",
    }


@pytest.fixture(scope="function")
def reset_container():
    """Reset the DI container before each test"""
    # Import here to avoid circular dependencies
    from faultmaven.container import container

    # Reset container state
    container.reset()

    # Ensure SKIP_SERVICE_CHECKS is set for tests
    os.environ["SKIP_SERVICE_CHECKS"] = "true"

    yield container

    # Reset again after test
    container.reset()


@pytest.fixture
def sample_session_context():
    """Sample session context for testing."""
    return SessionContext(
        session_id="test-session-123",
        user_id="user-456",
        created_at=datetime.now(),
        last_activity=datetime.now(),
        agent_state=create_agent_state_dict(),
        conversation_history=[],
        uploaded_data=[],
        insights={},
    )


@pytest.fixture
def sample_uploaded_data():
    """Sample uploaded data for testing."""
    return {
        "filename": "test.log",
        "data_type": DataType.SYSTEM_LOGS,
        "size": 1024,
        "uploaded_at": datetime.now(),
        "content": "2024-01-01 12:00:00 ERROR Test error",
    }


@pytest.fixture
def sample_processor_result():
    """Sample processor result for testing."""
    return Mock(
        summary="Test summary",
        insights={
            "error_count": 2,
            "error_rate": 0.4,
            "level_distribution": {"ERROR": 2, "INFO": 3},
            "time_range": {
                "start": "2024-01-01T12:00:00Z",
                "end": "2024-01-01T12:05:00Z",
            },
        },
        anomalies=[{"index": 5, "score": 0.9, "feature": "response_time"}],
        suggested_next_action="Investigate errors",
    )


@pytest.fixture
def mock_llm_router():
    """Mock LLM router for testing."""
    router = Mock()
    router.route.return_value = "Mocked LLM response"
    return router


@pytest.fixture
def mock_chroma_client():
    """Mock ChromaDB client for testing."""
    client = Mock()
    collection = Mock()
    client.get_collection.return_value = collection
    return client, collection


@pytest.fixture
def mock_session_manager():
    """Mock session manager for testing."""
    manager = Mock()
    manager.create_session.return_value = "test-session-id"
    manager.get_session.return_value = sample_session_context()
    manager.update_session.return_value = None
    return manager


@pytest.fixture
def mock_data_classifier():
    """Mock data classifier for testing."""
    classifier = Mock()
    classifier.classify.return_value = DataType.SYSTEM_LOGS
    return classifier


@pytest.fixture
def mock_log_processor():
    """Mock log processor for testing."""
    processor = Mock()
    processor.process.return_value = sample_processor_result()
    return processor


@pytest.fixture
def mock_data_sanitizer():
    """Mock data sanitizer for testing."""
    sanitizer = Mock()
    sanitizer.sanitize.return_value = "Sanitized content"
    sanitizer.is_sensitive.return_value = False
    return sanitizer


@pytest.fixture
def sample_log_data():
    """Sample log data for testing."""
    return """
2024-01-01 12:00:00 ERROR Database connection failed
2024-01-01 12:00:01 INFO Application started successfully
2024-01-01 12:00:02 WARN High memory usage detected
2024-01-01 12:00:03 ERROR Timeout occurred
2024-01-01 12:00:04 DEBUG Processing request
"""


@pytest.fixture
def sample_structured_logs():
    """Sample structured (JSON) logs for testing."""
    return """
{"timestamp": "2024-01-01T12:00:00Z", "level": "ERROR", "message": "DB error", "service": "api"}
{"timestamp": "2024-01-01T12:00:01Z", "level": "INFO", "message": "Request processed", "service": "api"}
{"timestamp": "2024-01-01T12:00:02Z", "level": "WARN", "message": "Slow query", "service": "db"}
{"timestamp": "2024-01-01T12:00:03Z", "level": "ERROR", "message": "Connection lost", "service": "api"}
"""


@pytest.fixture
def sample_knowledge_documents():
    """Sample knowledge base documents for testing."""
    return [
        {
            "document": "Database connection timeout troubleshooting guide",
            "metadata": {"source": "docs/troubleshooting.md", "type": "guide"},
            "distance": 0.1,
        },
        {
            "document": "How to configure connection pooling",
            "metadata": {"source": "docs/config.md", "type": "config"},
            "distance": 0.2,
        },
        {
            "document": "Common database errors and solutions",
            "metadata": {"source": "docs/errors.md", "type": "reference"},
            "distance": 0.3,
        },
    ]


@pytest.fixture
def mock_fireworks_client():
    """Mock Fireworks AI client for testing."""
    client = Mock()
    client.chat.completions.create.return_value = Mock(
        choices=[Mock(message=Mock(content="Fireworks AI response"))]
    )
    return client


@pytest.fixture
def mock_openrouter_client():
    """Mock OpenRouter client for testing."""
    client = Mock()
    client.chat.completions.create.return_value = Mock(
        choices=[Mock(message=Mock(content="OpenRouter response"))]
    )
    return client


@pytest.fixture
def mock_ollama_client():
    """Mock Ollama client for testing."""
    client = Mock()
    client.chat.completions.create.return_value = Mock(
        choices=[Mock(message=Mock(content="Ollama response"))]
    )
    return client


@pytest.fixture
def test_config():
    """Test configuration for FaultMaven."""
    return {
        "llm": {
            "fireworks": {"api_key": "test-key", "model": "test-model"},
            "openrouter": {"api_key": "test-key", "model": "test-model"},
            "ollama": {"base_url": "http://localhost:11434", "model": "llama2"},
        },
        "chromadb": {
            "persist_directory": "./test_chroma",
            "collection_name": "test_collection",
        },
        "session": {
            "timeout": 1800,  # 30 minutes for testing
            "cleanup_interval": 300,  # 5 minutes for testing
        },
        "security": {
            "secret_patterns": {
                "test_key": r"TEST_[A-Z0-9]{16}",
                "test_token": r"TEST_TOKEN_[A-Z0-9]{32}",
            }
        },
    }


# Case persistence fixtures
@pytest.fixture
def sample_case():
    """Sample case for testing case persistence functionality."""
    from faultmaven.modules.case.domain.models import Case, CaseState

    return Case(
        case_id="case_test12345678",
        title="Test Case for Persistence",
        description="A sample case for testing case persistence features",
        user_id="test-user-456",
        organization_id="test-org-123",
        state=CaseState.INQUIRY,
    )


@pytest.fixture
def sample_case_message():
    """Sample case message for testing."""
    from datetime import datetime, timezone

    from faultmaven.models.api_models import CaseMessage

    return CaseMessage(
        message_id="test-msg-123",
        case_id="case_test12345678",
        turn_number=1,
        role="user",
        content="This is a test message for case persistence testing",
        created_at=datetime.now(timezone.utc),
        author_id="test-user-456",
        metadata={"test": True, "source": "pytest"},
    )


@pytest.fixture
def sample_case_participant():
    """Sample case participant for testing."""
    from datetime import datetime, timezone

    from faultmaven.models.api_models import CaseParticipant

    return CaseParticipant(
        user_id="test-collaborator-789",
        role="collaborator",
        added_at=datetime.now(timezone.utc),
        added_by="test-user-456",
    )


@pytest.fixture
def sample_case_summary():
    """Sample case summary for testing list operations."""
    from datetime import datetime, timezone

    from faultmaven.models.api_models import CaseSummary
    from faultmaven.modules.case.domain.models import CaseState

    return CaseSummary(
        case_id="case_test12345678",
        title="Test Case Summary",
        state=CaseState.INQUIRY,
        user_id="test-user-456",
        organization_id="test-org-123",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
        current_turn=0,
        stage=None,  # INQUIRY: no investigation stage yet
        turns_without_progress=0,
        is_terminal=False,
    )


@pytest.fixture
def mock_case_store():
    """Mock case store for testing."""
    from unittest.mock import AsyncMock, Mock

    store = Mock()
    store.create_case = AsyncMock(return_value=True)
    store.get_case = AsyncMock(return_value=None)
    store.update_case = AsyncMock(return_value=True)
    store.delete_case = AsyncMock(return_value=True)
    store.list_cases = AsyncMock(return_value=[])
    store.search_cases = AsyncMock(return_value=[])
    store.add_message_to_case = AsyncMock(return_value=True)
    store.get_case_messages = AsyncMock(return_value=[])
    store.get_user_cases = AsyncMock(return_value=[])
    store.add_case_participant = AsyncMock(return_value=True)
    store.remove_case_participant = AsyncMock(return_value=True)
    store.update_case_activity = AsyncMock(return_value=True)
    store.cleanup_expired_cases = AsyncMock(return_value=0)
    store.get_case_analytics = AsyncMock(return_value={})
    return store


@pytest.fixture
def mock_case_service():
    """Mock case service for testing."""
    from unittest.mock import AsyncMock, Mock

    service = Mock()
    service.create_case = AsyncMock()
    service.get_case = AsyncMock(return_value=None)
    service.update_case = AsyncMock(return_value=False)
    service.add_message_to_case = AsyncMock(return_value=False)
    service.get_or_create_case_for_session = AsyncMock(return_value="test-case-123")
    service.link_session_to_case = AsyncMock(return_value=False)
    service.get_case_conversation_context = AsyncMock(return_value="")
    service.get_case_messages = AsyncMock(return_value=[])
    service.resume_case_in_session = AsyncMock(return_value=False)
    service.archive_case = AsyncMock(return_value=False)
    service.list_user_cases = AsyncMock(return_value=([], 0))
    service.search_cases = AsyncMock(return_value=[])
    service.get_case_analytics = AsyncMock(return_value={})
    service.cleanup_expired_cases = AsyncMock(return_value=0)
    service.hard_delete_case = AsyncMock(return_value=True)
    service.delete_case = AsyncMock(return_value=True)
    service.count_user_cases = AsyncMock(return_value=0)
    return service


@pytest.fixture
def case_create_request_data():
    """Sample case create request data for API testing."""
    return {
        "title": "Test Case Creation",
        "description": "Testing case creation via API",
        "priority": "medium",
        "tags": ["api", "test"],
        "session_id": "test-session-123",
        "initial_message": "Initial problem description for testing",
    }


@pytest.fixture
def case_update_request_data():
    """Sample case update request data for API testing."""
    return {
        "title": "Updated Test Case",
        "description": "Updated description for testing",
        "state": "investigating",
        "priority": "high",
        "tags": ["updated", "important"],
    }


@pytest.fixture
def case_share_request_data():
    """Sample case share request data for API testing."""
    return {
        "user_id": "test-collaborator-789",
        "role": "collaborator",
        "message": "Please help with this case",
    }


@pytest.fixture
def case_search_request_data():
    """Sample case search request data for API testing."""
    return {
        "query": "database connection error",
        "search_in_messages": True,
        "search_in_context": True,
        "filters": {"status": "active", "priority": "high", "limit": 20, "offset": 0},
    }


@pytest.fixture
def multiple_cases():
    """Multiple sample cases for testing list and search operations."""
    from faultmaven.modules.case.domain.models import Case, CaseState

    cases = []
    for i in range(5):
        case = Case(
            case_id=f"case_{i+1:012x}",
            title=f"Test Case {i+1}",
            description=f"Description for test case {i+1}",
            user_id=f"test-user-{i+1}",
            organization_id="test-org-123",
            state=CaseState.INQUIRY if i % 2 == 0 else CaseState.INVESTIGATING,
        )
        cases.append(case)

    return cases


@pytest.fixture
def case_with_conversation():
    """Sample case with a full conversation for testing context generation."""
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    from faultmaven.modules.case.domain.models import Case, CaseState

    now = datetime.now(timezone.utc)
    case_id = "case_conversation1"

    # Create messages as dicts per case-storage-design.md Section 4.7
    messages = [
        {
            "message_id": str(uuid4()),
            "case_id": case_id,
            "turn_number": 1,
            "role": "user",
            "content": "My application is crashing when users try to login",
            "created_at": (now - timedelta(minutes=60)).isoformat(),
        },
        {
            "message_id": str(uuid4()),
            "case_id": case_id,
            "turn_number": 1,
            "role": "assistant",
            "content": "I'll help you troubleshoot the login crashes. Can you provide the error logs?",
            "created_at": (now - timedelta(minutes=59)).isoformat(),
        },
        {
            "message_id": str(uuid4()),
            "case_id": case_id,
            "turn_number": 2,
            "role": "user",
            "content": "Here are the application logs from the past 24 hours",
            "created_at": (now - timedelta(minutes=55)).isoformat(),
        },
        {
            "message_id": str(uuid4()),
            "case_id": case_id,
            "turn_number": 2,
            "role": "assistant",
            "content": "I can see authentication service timeouts in the logs. Let me check the database connection pool.",
            "created_at": (now - timedelta(minutes=50)).isoformat(),
        },
        {
            "message_id": str(uuid4()),
            "case_id": case_id,
            "turn_number": 3,
            "role": "user",
            "content": "I've restarted the auth service but the issue persists",
            "created_at": (now - timedelta(minutes=30)).isoformat(),
        },
        {
            "message_id": str(uuid4()),
            "case_id": case_id,
            "turn_number": 3,
            "role": "assistant",
            "content": "The database connection pool seems to be exhausted. Try increasing the pool size from 10 to 50 connections.",
            "created_at": (now - timedelta(minutes=25)).isoformat(),
        },
    ]

    return Case(
        case_id=case_id,
        title="Case with Full Conversation",
        description="Testing conversation context generation",
        user_id="test-user-456",
        organization_id="test-org-123",
        state=CaseState.INVESTIGATING,
        messages=messages,
        message_count=len(messages),
        current_turn=3,
    )


# =============================================================================
# Personal-tenant retirement — shared fixtures (#1045 D8 R8)
# =============================================================================
#
# Four modules exercise this feature (the CLI's, the login path's, the WorkOS
# adapter's and the PostgreSQL one), and three of them had grown their own copy
# of the same tenant-context reset and their own recording double. One copy
# each, here, because a double that drifts between modules stops being evidence
# about the same thing.


@pytest.fixture
def restore_tenant_context():
    """Keep a bound organization from leaking into the next test.

    Deliberately NOT autouse at this scope: it resets a process-wide contextvar,
    and the modules that need it say so with ``pytestmark = pytest.mark
    .usefixtures("restore_tenant_context")`` rather than every test in the suite
    paying for a reset it never asked for.
    """
    from faultmaven.config.constants import STANDALONE_ORG_ID
    from faultmaven.config.tenant_context import set_current_org_id

    yield
    set_current_org_id(STANDALONE_ORG_ID)


class RecordingIdP:
    """The IdP teardown port, recording every call and its order.

    A hand-written double is right here because what the SDK signatures must be
    is pinned against the real classes with ``autospec`` in
    ``tests/unit/modules/auth/test_sso_personal_org_provider.py``. What the other
    modules need from the port is whether it was called, with which id, and when.

    ``present`` is the set of provider organization ids that still exist. A
    delete removes one — so a second delete of the same id reports it absent,
    exactly as WorkOS would, and a delete of an id that was never present cannot
    masquerade as success.
    """

    def __init__(self, present=(), *, error: Exception | None = None):
        from faultmaven.modules.auth.contracts import RetiredIdPOrganization

        self._result = RetiredIdPOrganization
        self.present = set(present)
        self.error = error
        self.calls: list[str] = []

    def retire_personal_organization(self, *, provider_org_id: str):
        self.calls.append(provider_org_id)
        if self.error is not None:
            raise self.error
        if provider_org_id not in self.present:
            return self._result(
                organization_absent=True,
                memberships_deleted=0,
                organization_deleted=False,
            )
        self.present.discard(provider_org_id)
        return self._result(
            organization_absent=False, memberships_deleted=1, organization_deleted=True
        )


class RecordingRevoker:
    """The auth service, for the one method a retirement uses."""

    def __init__(self, error: Exception | None = None):
        self.revoked: list[str] = []
        self.error = error

    async def revoke_user_tokens(self, user_id: str):
        if self.error is not None:
            raise self.error
        self.revoked.append(user_id)
        from datetime import UTC, datetime

        return datetime.now(UTC)


@pytest.fixture
def recording_idp():
    """Factory for :class:`RecordingIdP`, so a test names what exists."""
    return RecordingIdP


@pytest.fixture
def recording_revoker():
    return RecordingRevoker


@pytest.fixture
def anchor_db(monkeypatch):
    """A real ``enterprises`` table behind ``account_anchor``'s reads.

    The login's org-less verdict and its one anchor-mover both read typed
    columns — ``enterprises.deleted_at`` and
    ``enterprises.personal_tenant_retirement`` — so a unit test that wants to
    exercise them needs those rows to exist. Real SQLite built from the ORM
    metadata, with only the session factory patched, so the reads under test are
    the production ones rather than a stand-in that could disagree with them.

    Yields a seeder: ``seed(enterprise_id, retired=..., policy=...)``.
    """
    from contextlib import asynccontextmanager

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from faultmaven.infrastructure.persistence.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    created = {"done": False}

    @asynccontextmanager
    async def _session(database_url=None):
        if not created["done"]:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            created["done"] = True
        session = sessions()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.account_anchor.get_db_session", _session
    )

    async def seed(enterprise_id, *, retired=False, policy=None, name="Personal"):
        async with _session() as session:
            await session.execute(
                text(
                    "INSERT INTO enterprises (enterprise_id, name, slug, deleted_at, "
                    " personal_tenant_retirement) "
                    "VALUES (:e, :n, :s, :d, :p)"
                ),
                {
                    "e": enterprise_id,
                    "n": name,
                    "s": f"slug-{enterprise_id[:8]}",
                    "d": "2026-09-04 00:00:00" if retired else None,
                    "p": policy,
                },
            )

    yield seed
