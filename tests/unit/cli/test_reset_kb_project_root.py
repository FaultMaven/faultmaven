"""Moving ``reset_kb`` into the package did not move the tree it wipes (#887).

``fm-reset-kb`` derives ``data/chroma-kb`` from
``faultmaven.bootstrap.data_init.get_project_root()``. Relocating the module
from ``scripts/reset_kb.py`` to ``faultmaven/cli/reset_kb.py`` changed the
caller's depth in the tree, so the question was whether the resolution moved
with it. It does not: the file-relative fallback is anchored on
``data_init.py``'s own ``__file__``, so it is invariant under where the calling
module lives.

``get_project_root``'s three strategies are covered in
``tests/unit/bootstrap/test_get_project_root.py``.

**#936 moved the wipe off that anchor entirely.** ``get_project_root()`` now
locates repo-layout artifacts (``alembic.ini``, the KB pack) and nothing else;
the wipe path is ``CHROMADB_KB_PERSIST_DIR`` read exactly as
``create_persistent_client`` reads it, so the shipped relative default is
relative to the process's working directory. The structural claim above still
holds and is still worth pinning — the file-relative fallback is anchored on
``data_init.py``, not on its callers — but the cases below now place the store
by ``chdir`` rather than by ``PROJECT_ROOT``, because that is what decides it.
Which store the CLI picks, and its refusals, live in
``test_reset_kb_targets_the_servers_store.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultmaven.bootstrap import data_init
from faultmaven.cli import reset_kb

pytestmark = pytest.mark.unit


class _FakeResult:
    """Stands in for a DELETE result: the CLI reads ``.rowcount``."""

    def __init__(self, rowcount):
        self.rowcount = rowcount


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.deletes = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def scalar(self, _stmt):
        return self._rows

    async def execute(self, _stmt):
        self.deletes += 1
        return _FakeResult(self._rows)

    async def commit(self):
        return None


@pytest.fixture
def stub_db(monkeypatch):
    """Replace the session factory the CLI imports at call time."""

    def _stub(rows=0):
        session = _FakeSession(rows)
        from faultmaven.infrastructure.persistence import database

        monkeypatch.setattr(database, "get_db_session", lambda: session)
        return session

    return _stub


def test_the_cli_module_lives_under_the_root_data_init_resolves(tmp_path, monkeypatch):
    """The relocated module sits inside the same package tree the fallback
    names, so a caller in ``faultmaven/cli/`` resolves what one in ``scripts/``
    resolved."""
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)  # no marker files: force the file-relative path

    resolved = data_init.get_project_root().resolve()
    assert Path(reset_kb.__file__).resolve().parents[2] == resolved


async def test_the_banner_names_the_resolved_store_not_a_literal_path(
    tmp_path, monkeypatch, capsys, stub_db
):
    """The banner prints the absolute path it resolved. Which tree that is
    depends on PROJECT_ROOT and the working directory, and an operator cannot
    confirm the wipe targets the server's store unless the command says so."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "chroma-kb").mkdir(parents=True)
    stub_db(rows=7)

    code = await reset_kb.reset_kb(
        dry_run=True, all_drafts=False, rebuild=False, keep_chroma=False
    )

    assert code == 0
    out = capsys.readouterr().out
    # The ABSOLUTE path. The configured value is relative, and printing it back
    # verbatim ("data/chroma-kb") names no tree at all.
    assert str(tmp_path / "data" / "chroma-kb") in out
    assert "ChromaDB path exists: True" in out


async def test_a_missing_store_is_refused_not_wiped_and_warned_about(
    tmp_path, monkeypatch, capsys, stub_db
):
    """Finding no vector store means this process is not looking where the
    server looked. Until #936 the command deleted every row and *then* printed
    a DIVERGE warning — a description of damage, arriving after the
    irreversible half. It now refuses first and writes nothing."""
    monkeypatch.chdir(tmp_path)  # data/chroma-kb absent
    session = stub_db(rows=3)

    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=False
    )

    assert code == 1
    assert session.deletes == 0, "the SQL half must not run"
    out = capsys.readouterr().out
    assert "refusing" in out.lower()
    assert "DIVERGE" in out
    assert str(tmp_path / "data" / "chroma-kb") in out


async def test_keep_chroma_names_the_store_it_kept(
    tmp_path, monkeypatch, capsys, stub_db
):
    """--keep-chroma is not the missing-store case: nothing diverged, so no
    warning — but the path is still named."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "chroma-kb").mkdir(parents=True)
    stub_db(rows=3)

    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=True
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "WARNING" not in out
    assert (tmp_path / "data" / "chroma-kb").exists(), "the store must survive"
    assert str(tmp_path / "data" / "chroma-kb") in out


async def test_the_deleted_count_is_the_databases_rowcount(
    tmp_path, monkeypatch, capsys, stub_db
):
    """Reported counts come from the DELETE's own rowcount, so the number an
    operator reads is what the database did — not a pre-count that a concurrent
    writer could have made stale."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "chroma-kb").mkdir(parents=True)
    stub_db(rows=42)

    await reset_kb.reset_kb(
        dry_run=False, all_drafts=True, rebuild=False, keep_chroma=True
    )

    out = capsys.readouterr().out
    assert "Deleted 42 knowledge_items rows." in out
    assert "Deleted 42 conversion_drafts rows (--all-drafts)." in out
