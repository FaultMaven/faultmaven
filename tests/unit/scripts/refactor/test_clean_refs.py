"""Tests for scripts/refactor/clean_refs.py.

Each test builds a git repo with an old single-file module committed at
HEAD, a package split (the "homes"), and a caller tree, then runs the
checker (and, where relevant, --rewrite) against it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import commit_all, load_module, write

pytestmark = pytest.mark.unit

OLD_SRC = "def read_value():\n    return 1\n"


@pytest.fixture(scope="module")
def mod():
    return load_module("clean_refs")


def _base_split(repo: Path) -> str:
    """old/mod.py -> old/mod/{__init__,reader,user}.py. Returns old_rel."""
    old_rel = "old/mod.py"
    write(repo / old_rel, OLD_SRC)
    commit_all(repo)
    (repo / old_rel).unlink()
    pkg = repo / "old" / "mod"
    write(pkg / "__init__.py", '"""facade"""\n')
    write(pkg / "reader.py", "def read_value():\n    return 1\n")
    write(
        pkg / "user.py",
        "from old.mod.reader import read_value\n\n\ndef use_it():\n    return read_value() + 1\n",
    )
    return old_rel


def _run(
    mod,
    monkeypatch,
    capsys,
    repo: Path,
    old_rel: str,
    *extra: str,
    scan=("old", "callers"),
):
    argv = [
        "clean_refs.py",
        "--repo",
        str(repo),
        "--base",
        "HEAD",
        "--old",
        old_rel,
        "--scan",
        *scan,
        *extra,
    ]
    monkeypatch.setattr("sys.argv", argv)
    code = mod.main()
    return code, capsys.readouterr().out


class TestStaleImport:
    def test_a_stale_import_is_reported(self, mod, monkeypatch, capsys, repo):
        old_rel = _base_split(repo)
        write(
            repo / "callers/user.py",
            "from old.mod import read_value\n\n\ndef use():\n    return read_value()\n",
        )

        code, out = _run(mod, monkeypatch, capsys, repo, old_rel)

        assert code == 1
        assert "STALE-IMPORT" in out
        assert "old.mod.reader" in out

    def test_rewrite_fixes_it_to_the_defining_submodule(
        self, mod, monkeypatch, capsys, repo
    ):
        old_rel = _base_split(repo)
        caller = repo / "callers/user.py"
        write(
            caller,
            "from old.mod import read_value\n\n\ndef use():\n    return read_value()\n",
        )

        code, _ = _run(mod, monkeypatch, capsys, repo, old_rel, "--rewrite")
        assert code == 1  # the pre-rewrite report still names the finding

        text = caller.read_text()
        assert "from old.mod.reader import read_value" in text
        assert "from old.mod import read_value" not in text

        # And the rewritten tree is now clean.
        code2, out2 = _run(mod, monkeypatch, capsys, repo, old_rel)
        assert code2 == 0
        assert "RESULT: PASS" in out2


class TestReexport:
    def test_a_reexport_in_init_is_reported(self, mod, monkeypatch, capsys, repo):
        old_rel = _base_split(repo)
        write(
            repo / "old/mod/__init__.py",
            '"""facade"""\nfrom .reader import read_value\n',
        )

        code, out = _run(mod, monkeypatch, capsys, repo, old_rel)

        assert code == 1
        assert "REEXPORT" in out
        assert "old/mod/__init__.py" in out


class TestUnusedImport:
    def test_an_unused_import_in_a_home_module_is_reported(
        self, mod, monkeypatch, capsys, repo
    ):
        old_rel = _base_split(repo)
        write(
            repo / "old/mod/reader.py",
            "import os\n\n\ndef read_value():\n    return 1\n",
        )

        code, out = _run(mod, monkeypatch, capsys, repo, old_rel)

        assert code == 1
        assert "UNUSED-IMPORT" in out
        assert "os" in out


class TestAliasAttr:
    def test_alias_attr_through_a_package_alias_is_reported(
        self, mod, monkeypatch, capsys, repo
    ):
        old_rel = _base_split(repo)
        write(
            repo / "callers/c1.py",
            "import old.mod as m\n\n\ndef f():\n    return m.read_value()\n",
        )

        code, out = _run(mod, monkeypatch, capsys, repo, old_rel)

        assert code == 1
        assert "STALE-ATTR" in out
        assert "m.read_value" in out

    def test_alias_dunder_name_is_not_reported(self, mod, monkeypatch, capsys, repo):
        old_rel = _base_split(repo)
        write(
            repo / "callers/c2.py",
            "import old.mod as m\n\n\ndef g():\n    return m.__name__\n",
        )

        code, out = _run(mod, monkeypatch, capsys, repo, old_rel)

        assert code == 0, out
        assert "STALE-ATTR" not in out


class TestLegitimatePatch:
    def test_a_patch_of_a_name_the_module_imports_and_uses_is_not_reported(
        self, mod, monkeypatch, capsys, repo
    ):
        old_rel = _base_split(repo)
        # The facade itself imports AND uses read_value: patch("old.mod.read_value")
        # targets a namespace that genuinely reads the name, so it is legitimate
        # even though old.mod is not the defining module.
        write(
            repo / "old/mod/__init__.py",
            '"""facade"""\nfrom old.mod.reader import read_value\n\n\ndef convenience():\n    return read_value()\n',
        )
        write(
            repo / "callers/c3.py",
            'from unittest.mock import patch\n\n\ndef test_patch():\n    with patch("old.mod.read_value", return_value=2):\n        pass\n',
        )

        code, out = _run(mod, monkeypatch, capsys, repo, old_rel, "--allow-init-code")

        assert code == 0, out
        assert "STALE-STRING" not in out


class TestDocDotted:
    def test_a_doc_dotted_reference_is_reported_as_warn_doc_dotted(
        self, mod, monkeypatch, capsys, repo
    ):
        old_rel = _base_split(repo)
        write(repo / "docs/notes.md", "See old.mod.read_value for details.\n")

        code, out = _run(
            mod, monkeypatch, capsys, repo, old_rel, scan=("old", "callers", "docs")
        )

        # WARN-* never fails the run on its own; only assert it is reported.
        assert "WARN-DOC-DOTTED" in out
        assert "old.mod.reader.read_value" in out
