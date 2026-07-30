"""``get_project_root`` resolves the tree the deployment actually writes to.

Everything that touches ``data/`` — the directory bootstrap, the migration
runner, and ``fm-reset-kb``'s ChromaDB wipe — derives its path from here, so
resolving the wrong root means creating or *deleting* the wrong tree.

Three strategies, in order: ``PROJECT_ROOT``; then a working directory holding
``alembic.ini`` or ``pyproject.toml`` (the container's ``WORKDIR=/app`` holds
both, which is how a pod resolves it); then the location of ``data_init.py``
itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultmaven.bootstrap import data_init
from faultmaven.bootstrap.data_init import get_project_root

pytestmark = pytest.mark.unit


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    # Even with a perfectly good marker in the working directory.
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "alembic.ini").write_text("[alembic]\n")
    monkeypatch.chdir(tmp_path / "other")

    assert get_project_root() == tmp_path


@pytest.mark.parametrize("marker", ["alembic.ini", "pyproject.toml"])
def test_either_marker_makes_the_working_directory_the_root(
    tmp_path, monkeypatch, marker
):
    """Strategy 2 accepts *either* marker — the image's WORKDIR has both, but a
    checkout run from the repo root may be matched by only one of them."""
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    (tmp_path / marker).write_text("")
    monkeypatch.chdir(tmp_path)

    assert get_project_root() == tmp_path


def test_file_relative_fallback_is_anchored_on_data_init(tmp_path, monkeypatch):
    """Strategy 3 is anchored on ``data_init.py``'s own ``__file__``, never the
    caller's — which is why moving a caller between directories cannot shift it
    (#887 relocated ``reset_kb`` from ``scripts/`` into ``faultmaven/cli/``)."""
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)  # no alembic.ini / pyproject.toml here

    expected = Path(data_init.__file__).resolve().parents[2]
    assert get_project_root().resolve() == expected
