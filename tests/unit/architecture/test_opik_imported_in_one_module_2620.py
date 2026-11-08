"""The trace SDK is imported in one module only (#2616 part (c)).

Principle 13.4's enforcement row (``architectural-design-principles.md``): the
composition root sets a third-party SDK's process-wide policy once, before the
SDK is imported, which holds only while one module imports it. That module,
``opik_sdk``, defaults ``OPIK_SENTRY_ENABLE`` before its ``import opik``; any
other importer that runs first starts Comet's Sentry (#2585) and decides the
SDK's policy on its own. That recurred: #1259 unified the availability check,
and importers came back after it, #2578's lazy imports in ``trace_erasure.py``,
beside ``reset_kb.py``'s own direct initialisation.

Every spelling counts, a function body's lazy import included: ``import opik``,
``import opik.config``, ``from opik.x import y`` and a literal
``import_module("opik...")``.
"""

import pathlib

import pytest

from tests.import_guard_ast import imported_modules
from tests.source_tree import PACKAGE_ROOT, parse_file

pytestmark = pytest.mark.unit

SDK = "opik"
OWNER = PACKAGE_ROOT / "infrastructure" / "observability" / "opik_sdk.py"


def _importers(paths) -> dict[pathlib.Path, list[int]]:
    found: dict[pathlib.Path, list[int]] = {}
    for path in paths:
        for name, line in imported_modules(parse_file(path)):
            if name.split(".")[0] == SDK:
                found.setdefault(path, []).append(line)
    return found


def test_only_opik_sdk_imports_the_trace_sdk():
    files = sorted(PACKAGE_ROOT.rglob("*.py"))
    # A scan that walked nothing would pass.
    assert len(files) >= 200 and OWNER in files

    importers = _importers(files)

    assert list(importers) == [OWNER], {
        str(path.relative_to(PACKAGE_ROOT.parent)): lines
        for path, lines in importers.items()
        if path != OWNER
    }


@pytest.mark.parametrize(
    "source",
    [
        "import opik\n",
        "import opik.config\n",
        "import opik as sdk\n",
        "from opik import opik_context\n",
        "from opik.api_objects.opik_client import get_client_cached\n",
        "def lazy():\n    from opik.rest_api.core.api_error import ApiError\n",
        "import importlib\nimportlib.import_module('opik.integrations.fastapi')\n",
        "__import__('opik')\n",
    ],
)
def test_the_scan_catches_every_spelling(tmp_path, source):
    probe = tmp_path / "probe_module.py"
    probe.write_text(source)

    assert list(_importers([probe])) == [probe]


@pytest.mark.parametrize(
    "source",
    [
        "from faultmaven.infrastructure.observability import opik_sdk\n",
        "import opik_sdk\n",
        "from . import opik\n",
        "x = 'import opik'\n",
    ],
)
def test_the_scan_leaves_other_names_alone(tmp_path, source):
    probe = tmp_path / "probe_module.py"
    probe.write_text(source)

    assert _importers([probe]) == {}
