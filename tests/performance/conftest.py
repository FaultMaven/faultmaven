"""Mark every test here ``performance`` (#2090), a new module included."""

from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    for item in items:
        if _HERE in Path(item.path).resolve().parents:
            item.add_marker(pytest.mark.performance)
