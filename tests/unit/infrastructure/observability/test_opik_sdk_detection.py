"""A namespace-package shadow must not count as an installed Opik SDK.

Why an empty directory imports at all is documented once beside the guard
itself — see ``faultmaven/utils/optional_dependency.py``. This file pins the one
``import opik`` site: ``opik_sdk.OPIK_AVAILABLE``, which the middleware branch
and ``OpikTracer.health_check`` read, and ``SPAN_TAGGING_AVAILABLE``, which also
needs the span writers and which the router's and the classifier's decorators
read (#2616 part c).

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


# What the probe reports by default: the two flags of ``opik_sdk``.
_AVAILABILITY = """
from faultmaven.infrastructure.observability import opik_sdk

result = {
    "available": opik_sdk.OPIK_AVAILABLE,
    "middleware": opik_sdk.fastapi_middleware() is not None,
}
"""


def _probe(substitute: str, report: str = _AVAILABILITY) -> dict:
    """Report the guard from a fresh interpreter, with `substitute` installed.

    A subprocess because the stand-in ``opik`` has to be in ``sys.modules``
    before the modules under test import it, and they read it only once, at
    import. Substituting there rather than shadowing ``sys.path`` keeps the
    result identical whether or not the real SDK is installed: a path-shadowing
    probe would place an empty dir on PYTHONPATH, namespace scanning would
    continue past it to the real ``opik/__init__.py`` in site-packages, and on
    CI the test would silently measure the real package and pass vacuously.

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
{report}
with open(OUT, "w") as f:
    json.dump(result, f)
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
    assert result["available"] is False
    assert result["middleware"] is False


def test_installed_sdk_is_detected():
    result = _probe(_REAL)
    assert result["available"] is True
    # The middleware lives in a submodule, which the stand-in does not have, so
    # only the top-level flag flips. Asserted rather than merely described:
    # it is what keeps the negative test above measuring two separate facts.
    assert result["middleware"] is False


# An installed SDK whose ``opik_context`` has, or has lost, the span writers.
_CONTEXT = (
    _REAL
    + "\nopik.track = lambda **options: (lambda func: func)"
    + '\ncontext = types.ModuleType("opik.opik_context")'
    + '\ncontext.__file__ = "/nonexistent/site-packages/opik/opik_context.py"'
    + "\n{writers}"
    + "\nopik.opik_context = context"
    + '\nsys.modules["opik.opik_context"] = context'
)

_DECORATED = """
from faultmaven.infrastructure.llm.router import _opik_track_llm
from faultmaven.infrastructure.observability import opik_sdk
from faultmaven.modules.preprocessing.classifier import _opik_track_classifier


async def routed():
    return None


def classified():
    return None


result = {
    "available": opik_sdk.OPIK_AVAILABLE,
    "span_tagging": opik_sdk.SPAN_TAGGING_AVAILABLE,
    "router": _opik_track_llm("probe")(routed) is not routed,
    "classifier": _opik_track_classifier(classified) is not classified,
}
"""


@pytest.mark.parametrize(
    "writers, decorated",
    [
        ("", False),
        ("context.update_current_span = lambda **fields: None", False),
        (
            "context.update_current_span = context.update_current_trace = "
            "lambda **fields: None",
            True,
        ),
    ],
    ids=["no-writers", "no-trace-writer", "both-writers"],
)
def test_an_sdk_without_the_span_writers_decorates_no_call(writers, decorated):
    """A span the router cannot tag with its owner is one the case delete cannot
    find (#2102), so neither call site is decorated without both writers."""
    result = _probe(_CONTEXT.format(writers=writers), report=_DECORATED)

    assert result == {
        "available": True,
        "span_tagging": decorated,
        "router": decorated,
        "classifier": decorated,
    }


_EFFECTIVE = """
import asyncio

from faultmaven.infrastructure.health.component_monitor import ComponentHealthMonitor
from faultmaven.infrastructure.observability import opik_sdk

opik_sdk._record = opik_sdk.TraceBackend(
    sdk_available=True, enabled=True, configured=True, project="faultmaven"
)
health = asyncio.run(ComponentHealthMonitor()._check_tracer_health())

result = {
    "configured": opik_sdk.trace_backend_configured(),
    "effective": opik_sdk.tracing_is_effective(),
    "tracer": health["status"].value,
}
"""


@pytest.mark.parametrize(
    "writers, effective",
    [
        ("", False),
        ("context.update_current_span = lambda **fields: None", False),
        (
            "context.update_current_span = context.update_current_trace = "
            "lambda **fields: None",
            True,
        ),
    ],
    ids=["no-writers", "no-trace-writer", "both-writers"],
)
def test_without_the_span_writers_a_configured_backend_records_nothing(
    writers, effective
):
    """#2629's delta K1: with no call decorated, a configured, enabled backend
    must not read as tracing (#1234), and the tracer probe reads DEGRADED. The
    eraser's own gate still answers True: written traces stay deletable."""
    switched_on = _CONTEXT + "\nopik.is_tracing_active = lambda: True"

    result = _probe(switched_on.format(writers=writers), report=_EFFECTIVE)

    assert result == {
        "configured": True,
        "effective": effective,
        "tracer": "healthy" if effective else "degraded",
    }
