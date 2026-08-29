"""A namespace-package shadow must not count as an installed Opik SDK.

``opik`` is optional (pyproject's ``[cloud]`` extra). pip's uninstall removes a
package's files but leaves its directories, so an environment that once had the
extra keeps an empty ``site-packages/opik/`` tree — and PEP 420 resolves that to
a namespace package: ``import opik`` succeeds, ``find_spec("opik")`` returns a
spec, and none of the SDK exists. Every FaultMaven site that decided
"is Opik installed?" from a bare ``import opik`` therefore answered yes to an
empty directory, reporting ``opik_sdk_available: True`` and logging
"Opik SDK available but middleware not found" with no SDK present.

The sites that additionally from-import a symbol (``from opik import
opik_context`` in llm/router.py and preprocessing/classifier.py, ``from opik
import track`` in shims/observability.py) already raise ImportError on a
namespace package and were never affected. These tests cover the two
bare-import sites.

The discriminator is that a namespace package has no ``__file__`` (and its spec
no ``origin``), which the first two tests pin against real directories rather
than assuming it — the rest of the file rests on that fact.
"""

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]


def test_empty_directory_imports_as_namespace_package_with_no_file(
    tmp_path, monkeypatch
):
    """The fact the guards rely on: an empty directory is importable, and the
    module it produces has ``__file__ is None`` / ``spec.origin is None``."""
    (tmp_path / "_fm_ns_probe").mkdir()
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    spec = importlib.util.find_spec("_fm_ns_probe")
    assert spec is not None  # importable — which is why find_spec is not enough
    assert spec.origin is None

    module = importlib.import_module("_fm_ns_probe")
    try:
        assert getattr(module, "__file__", None) is None
    finally:
        sys.modules.pop("_fm_ns_probe", None)


def test_file_backed_package_has_file_and_origin(tmp_path, monkeypatch):
    """The other half: a real package sets both, so the guards stay open for
    an actually-installed SDK."""
    package = tmp_path / "_fm_real_probe"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    spec = importlib.util.find_spec("_fm_real_probe")
    assert spec is not None and spec.origin is not None

    module = importlib.import_module("_fm_real_probe")
    try:
        assert getattr(module, "__file__", None) is not None
    finally:
        sys.modules.pop("_fm_real_probe", None)


def _probe(body: str) -> dict:
    """Run `body` in a fresh interpreter and return the JSON it writes to OUT.

    A subprocess because the substitute ``opik`` goes into ``sys.modules``
    before the module under test imports it, and because the modules under test
    read that only once, at import. Substituting there rather than shadowing
    ``sys.path`` keeps the result identical whether or not the real SDK is
    installed — on CI it is, and a path-shadowing probe would silently measure
    the real package instead.

    Runs in a temporary cwd (the app writes ``data/`` on settings bootstrap)
    and pins ``faultmaven`` to this checkout, since the editable install
    resolves it regardless of where the probe runs.
    """
    pin = (
        "import faultmaven as _fm, pathlib as _pl\n"
        f"_root = _pl.Path({str(REPO_ROOT)!r}).resolve()\n"
        "_loaded = _pl.Path(_fm.__file__).resolve()\n"
        "assert _root in _loaded.parents, (\n"
        "    'probe loaded faultmaven from %s, not the tree under test (%s)'\n"
        "    % (_loaded, _root)\n"
        ")\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "probe.json"
        result = subprocess.run(
            [sys.executable, "-c", f"OUT = {str(out)!r}\n" + pin + body],
            capture_output=True,
            text=True,
            cwd=tmp,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            timeout=300,
        )
        assert result.returncode == 0, (
            f"probe subprocess failed:\n--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
        return json.loads(out.read_text())


# ``__file__ = None`` and ``__path__ = []`` are exactly what CPython gives a PEP
# 420 namespace package (pinned by the two tests above); the real one exposes
# none of the SDK either, so the substitute needs no attributes.
_SHADOW = """
import sys, types

shadow = types.ModuleType("opik")
shadow.__file__ = None
shadow.__path__ = []
sys.modules["opik"] = shadow
"""

# The same substitute with a ``__file__``, standing in for an installed SDK. It
# is the positive control: without it, "OPIK_AVAILABLE is False" would also hold
# for a guard that had simply stopped detecting Opik at all.
_REAL = """
import sys, types

real = types.ModuleType("opik")
real.__file__ = "/nonexistent/site-packages/opik/__init__.py"
real.__path__ = ["/nonexistent/site-packages/opik"]
sys.modules["opik"] = real
"""

_TRACING_BODY = """
import json

from faultmaven.infrastructure.observability import tracing

with open(OUT, "w") as f:
    json.dump({"opik_available": tracing.OPIK_AVAILABLE}, f)
"""

_MAIN_BODY = """
import json

import faultmaven.main as main

with open(OUT, "w") as f:
    json.dump(
        {
            "opik_available": main.OPIK_AVAILABLE,
            "middleware_available": main.OPIK_MIDDLEWARE_AVAILABLE,
        },
        f,
    )
"""


def test_tracing_reports_namespace_shadow_as_unavailable():
    """tracing.OPIK_AVAILABLE gates the log line claiming tracing was
    initialized and the ``opik_sdk_available`` field OpikTracer.health_check
    publishes; an empty directory must not turn either on."""
    assert _probe(_SHADOW + _TRACING_BODY)["opik_available"] is False


def test_tracing_reports_installed_sdk_as_available():
    assert _probe(_REAL + _TRACING_BODY)["opik_available"] is True


def test_main_reports_namespace_shadow_as_unavailable():
    """main.OPIK_AVAILABLE decides between "Opik not available" and "Opik SDK
    available but middleware not found" — the second is the misleading one."""
    result = _probe(_SHADOW + _MAIN_BODY)
    assert result["opik_available"] is False
    assert result["middleware_available"] is False


def test_main_reports_installed_sdk_as_available():
    # The middleware lives in a submodule of the substitute, which has none, so
    # only the top-level flag flips — the split this test needs to stay honest.
    assert _probe(_REAL + _MAIN_BODY)["opik_available"] is True
