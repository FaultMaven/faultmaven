"""A namespace-package shadow must not count as an installed Opik SDK.

Why an empty directory imports at all, and which sites were affected, is
documented once beside the guard itself — see
``faultmaven/infrastructure/observability/tracing.py``. This file pins the two
bare-``import opik`` sites that guard reaches: ``tracing.OPIK_AVAILABLE``
(which feeds ``OpikTracer.health_check``'s ``opik_sdk_available``) and
``main.OPIK_AVAILABLE`` (which picks the startup log line).

The first test pins the CPython fact the guards rest on rather than assuming
it, and pins it precisely: a namespace package *sets* ``__file__`` to None
rather than omitting it, which is what makes either spelling of the check safe.
"""

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


@pytest.mark.parametrize(
    "init_py, expect_file",
    [
        pytest.param(None, False, id="empty-dir-is-namespace-package"),
        pytest.param("VALUE = 1\n", True, id="file-backed-is-real-package"),
    ],
)
def test_only_a_file_backed_package_sets_file_and_origin(
    tmp_path, monkeypatch, init_py, expect_file
):
    """The discriminator: an empty directory is importable but sets neither
    ``spec.origin`` nor ``__file__``; a real package sets both."""
    name = "_fm_ns_probe" if init_py is None else "_fm_real_probe"
    package = tmp_path / name
    package.mkdir()
    if init_py is not None:
        (package / "__init__.py").write_text(init_py)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    spec = importlib.util.find_spec(name)
    assert spec is not None  # importable either way — why find_spec is not enough
    assert (spec.origin is not None) is expect_file

    module = importlib.import_module(name)
    try:
        # `hasattr` separately from the value: a namespace package SETS
        # __file__ to None rather than omitting it, so `opik.__file__ is None`
        # would be safe too. Pinned because the guards ship the getattr form
        # and this is what says the plain attribute access is not a landmine.
        assert hasattr(module, "__file__")
        assert (module.__file__ is not None) is expect_file
    finally:
        sys.modules.pop(name, None)


def _probe(substitute: str) -> dict:
    """Report both guards from one interpreter, with `substitute` installed.

    A subprocess because the stand-in ``opik`` has to be in ``sys.modules``
    before the modules under test import it, and they read it only once, at
    import. Substituting there rather than shadowing ``sys.path`` keeps the
    result identical whether or not the real SDK is installed: a path-shadowing
    probe would place an empty dir on PYTHONPATH, namespace scanning would
    continue past it to the real ``opik/__init__.py`` in site-packages, and on
    CI the test would silently measure the real package and pass vacuously.

    One interpreter for both flags: ``faultmaven.main`` already imports
    ``tracing``, so a separate tracing-only probe would pay a second process
    for a fact this one already has.

    Runs in a temporary cwd (importing the app bootstraps settings, which
    writes ``data/``) and pins ``faultmaven`` to this checkout, since the
    editable install resolves it regardless of where the probe runs.
    """
    body = f"""
import sys, types

opik = types.ModuleType("opik")
{substitute}
sys.modules["opik"] = opik

import json

from faultmaven.infrastructure.observability import tracing
import faultmaven.main as main

with open(OUT, "w") as f:
    json.dump(
        {{
            "tracing_available": tracing.OPIK_AVAILABLE,
            "main_available": main.OPIK_AVAILABLE,
            "main_middleware_available": main.OPIK_MIDDLEWARE_AVAILABLE,
        }},
        f,
    )
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
        assert out.exists(), (
            f"probe wrote no result:\n--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
        return json.loads(out.read_text())


# What CPython gives a PEP 420 namespace package, pinned by the test above. The
# real one exposes none of the SDK either, so the stand-in needs no attributes.
_SHADOW = "opik.__file__ = None\nopik.__path__ = []"

# The same stand-in with a __file__, for an installed SDK. It is the positive
# control: without it, "OPIK_AVAILABLE is False" would also hold for a guard
# that had simply stopped detecting Opik at all.
_REAL = (
    'opik.__file__ = "/nonexistent/site-packages/opik/__init__.py"\n'
    'opik.__path__ = ["/nonexistent/site-packages/opik"]'
)


def test_namespace_shadow_is_not_an_installed_sdk():
    result = _probe(_SHADOW)
    assert result["tracing_available"] is False
    assert result["main_available"] is False
    assert result["main_middleware_available"] is False


def test_installed_sdk_is_detected():
    result = _probe(_REAL)
    assert result["tracing_available"] is True
    assert result["main_available"] is True
    # The middleware lives in a submodule, which the stand-in does not have, so
    # only the top-level flag flips. Asserted rather than merely described:
    # it is what keeps the negative test above measuring two separate facts.
    assert result["main_middleware_available"] is False
