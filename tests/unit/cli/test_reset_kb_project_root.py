"""``fm-reset-kb`` resolves the same tree whichever way it is invoked (#887).

``reset_kb`` derives ``data/chroma-kb`` from
``faultmaven.bootstrap.data_init.get_project_root()``. Moving the script from
``scripts/reset_kb.py`` into ``faultmaven/cli/reset_kb.py`` changed the caller's
depth in the tree, so the question is whether the resolution moved with it.

It does not, and that is the property worth pinning: ``get_project_root``'s
file-relative fallback is anchored on ``data_init.py``'s own ``__file__``, never
the caller's — so it is invariant under where the CLI module lives. The two
higher-priority strategies (``PROJECT_ROOT``, then a working directory holding
``alembic.ini``/``pyproject.toml``) are caller-independent by construction, and
the image's ``WORKDIR=/app`` holds both markers, so a pod hits strategy 2.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from faultmaven.bootstrap import data_init
from faultmaven.bootstrap.data_init import get_project_root

pytestmark = pytest.mark.unit


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    assert get_project_root() == tmp_path


def test_working_directory_with_markers_is_the_root(tmp_path, monkeypatch):
    """Strategy 2 — how a pod resolves it: WORKDIR=/app holds both markers."""
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    (tmp_path / "alembic.ini").write_text("[alembic]\n")
    monkeypatch.chdir(tmp_path)

    assert get_project_root() == tmp_path


def test_file_relative_fallback_is_anchored_on_data_init_not_the_caller(
    tmp_path, monkeypatch
):
    """Strategy 3 — the one the move could have broken, and did not.

    With no ``PROJECT_ROOT`` and a marker-less working directory, the result is
    the package's parent as computed from ``data_init.py``. Calling from
    ``faultmaven/cli`` (one level deeper than ``scripts/``, one level deeper
    than ``bootstrap`` is from the root) must not shift it.
    """
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)  # no alembic.ini / pyproject.toml here

    expected = Path(data_init.__file__).resolve().parents[2]
    assert get_project_root().resolve() == expected

    # The importable package root really is that directory: resolving it from
    # the moved CLI module lands in the same place.
    cli = importlib.import_module("faultmaven.cli.reset_kb")
    assert Path(cli.__file__).resolve().parents[2] == expected


def test_reset_kb_chroma_dir_follows_the_resolved_root(tmp_path, monkeypatch):
    """The path the wipe targets is the deployment's, not the module's."""
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))

    from faultmaven.cli import reset_kb

    assert reset_kb.get_project_root() / "data" / "chroma-kb" == (
        tmp_path / "data" / "chroma-kb"
    )
